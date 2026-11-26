import hashlib
import hmac
import json
import math
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import time
from contextlib import contextmanager
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("PLAYSHOP_DATABASE", ROOT / "playshop.db"))
HOST = os.environ.get("PLAYSHOP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLAYSHOP_PORT", "3000"))
MAX_BODY_SIZE = 1_000_000
PASSWORD_ITERATIONS = 240_000
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
INITIAL_PRODUCTS = [
    ("God of War Ragnarök", "Uma jornada épica com ação intensa, narrativa envolvente e desafios grandiosos.", "https://images.igdb.com/igdb/image/upload/t_cover_big/coba3d.webp", "Ação", "PS5", 24990, 12),
    ("FIFA 25", "Experimente partidas realistas, times icônicos e uma temporada repleta de emoção.", "https://images.igdb.com/igdb/image/upload/t_cover_big/coa755.webp", "Esporte", "PC", 19990, 25),
    ("Cyberpunk 2077", "Explore um mundo futurista cheio de missões, escolhas e uma história envolvente.", "https://images.igdb.com/igdb/image/upload/t_cover_big/coaih8.webp", "RPG", "Xbox", 17990, 18),
    ("Zelda: Tears of the Kingdom", "Uma aventura magnífica com exploração, quebra-cabeças e mundo aberto impressionante.", "https://images.igdb.com/igdb/image/upload/t_cover_big/co5vmg.webp", "Aventura", "Nintendo", 28990, 9),
    ("Fortnite", "Battle royale dinâmico, em constante atualização e cheio de eventos especiais.", "https://images.igdb.com/igdb/image/upload/t_cover_big/cocxbi.webp", "Battle Royale", "Multiplataforma", 14990, 31),
    ("Minecraft", "Crie, construa e sobreviva em um mundo virtual infinitamente criativo.", "https://images.igdb.com/igdb/image/upload/t_cover_big/co8fu7.webp", "Sobrevivência", "PC", 9990, 14),
    ("Elden Ring", "Um mundo desafiador, repleto de mistérios, bosses gigantes e exploração fascinante.", "https://images.igdb.com/igdb/image/upload/t_cover_big/co4jni.png", "RPG", "PC", 25990, 10),
    ("The Last of Us Part II", "Uma experiência intensa de narrativa, sobrevivência e escolhas emocionais.", "https://images.igdb.com/igdb/image/upload/t_cover_big/co5ziw.webp", "Sobrevivência", "PS5", 21990, 11),
    ("Halo Infinite", "Combate inimigos em um universo épico com gráficos impressionantes e ação intensa.", "https://images.igdb.com/igdb/image/upload/t_cover_big/co2dto.webp", "FPS", "Xbox", 19990, 20),
]


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@contextmanager
def connect_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    with connect_database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                descricao TEXT NOT NULL,
                imagem TEXT NOT NULL,
                categoria TEXT NOT NULL,
                plataforma TEXT NOT NULL,
                preco_centavos INTEGER NOT NULL CHECK (preco_centavos >= 0),
                estoque INTEGER NOT NULL CHECK (estoque >= 0)
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                cpf TEXT NOT NULL,
                telefone TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS password_resets (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                email TEXT NOT NULL DEFAULT '',
                address_json TEXT NOT NULL,
                total_centavos INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                product_name TEXT NOT NULL,
                product_image TEXT NOT NULL DEFAULT '',
                unit_price_centavos INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0)
            );
            """
        )
        order_item_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(order_items)")
        }
        if "product_image" not in order_item_columns:
            connection.execute(
                "ALTER TABLE order_items ADD COLUMN product_image TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                """
                UPDATE order_items
                SET product_image = COALESCE(
                    (SELECT imagem FROM products WHERE products.id = order_items.product_id),
                    ''
                )
                """
            )
        count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count == 0:
            connection.executemany(
                """
                INSERT INTO products
                    (nome, descricao, imagem, categoria, plataforma, preco_centavos, estoque)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                INITIAL_PRODUCTS,
            )


def product_json(row):
    return {
        "id": row["id"],
        "nome": row["nome"],
        "descricao": row["descricao"],
        "imagem": row["imagem"],
        "categoria": row["categoria"],
        "plataforma": row["plataforma"],
        "preco": row["preco_centavos"] / 100,
        "estoque": row["estoque"],
    }


def validate_product(data):
    text_fields = ("nome", "descricao", "imagem", "categoria", "plataforma")
    if not isinstance(data, dict):
        raise ApiError(400, "Envie os dados do produto em formato JSON.")
    result = {}
    for field in text_fields:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, f"O campo '{field}' é obrigatório.")
        result[field] = value.strip()
    price = data.get("preco")
    stock = data.get("estoque")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
        raise ApiError(400, "O preço deve ser um número igual ou maior que zero.")
    if isinstance(stock, bool) or not isinstance(stock, int) or stock < 0:
        raise ApiError(400, "O estoque deve ser um número inteiro igual ou maior que zero.")
    result["preco_centavos"] = round(price * 100)
    result["estoque"] = stock
    return result


def password_digest(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_ITERATIONS
    ).hex()


def public_user(row):
    return {"id": row["id"], "nome": row["nome"], "email": row["email"]}


class PlayShopHandler(BaseHTTPRequestHandler):
    server_version = "PlayShopAPI/1.0"

    def log_message(self, format_string, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), format_string % args))

    def send_json(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        origin = self.headers.get("Origin", "")
        if origin in ("http://localhost:4200", "http://127.0.0.1:4200"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Admin-Key")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Cabeçalho de tamanho inválido.")
        if length < 1 or length > MAX_BODY_SIZE:
            raise ApiError(400, "Corpo da requisição ausente ou muito grande.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "O corpo da requisição deve ser um JSON válido.")
        if not isinstance(value, dict):
            raise ApiError(400, "O corpo da requisição deve ser um objeto JSON.")
        return value

    def require_admin(self):
        expected = os.environ.get("PLAYSHOP_ADMIN_KEY", "")
        supplied = self.headers.get("X-Admin-Key", "")
        if not expected:
            raise ApiError(503, "Configure PLAYSHOP_ADMIN_KEY para habilitar o gerenciamento de produtos.")
        if not hmac.compare_digest(supplied, expected):
            raise ApiError(403, "Chave administrativa inválida.")

    def authenticated_user(self, required=False):
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not token:
            if required:
                raise ApiError(401, "Faça login para acessar este recurso.")
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with connect_database() as connection:
            row = connection.execute(
                """
                SELECT users.* FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (token_hash, int(time.time())),
            ).fetchone()
        if row is None:
            if required:
                raise ApiError(401, "Sua sessão expirou. Entre novamente.")
            return None
        return row

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin in ("http://localhost:4200", "http://127.0.0.1:4200"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Admin-Key")
        self.end_headers()

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path == "/api/health":
                self.send_json(200, {"status": "ok"})
            elif path == "/api/products":
                with connect_database() as connection:
                    rows = connection.execute("SELECT * FROM products ORDER BY id").fetchall()
                self.send_json(200, [product_json(row) for row in rows])
            elif re.fullmatch(r"/api/products/\d+", path):
                product_id = int(path.rsplit("/", 1)[1])
                with connect_database() as connection:
                    row = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
                if row is None:
                    raise ApiError(404, "Produto não encontrado.")
                self.send_json(200, product_json(row))
            elif path == "/api/auth/me":
                user = self.authenticated_user(required=True)
                self.send_json(200, public_user(user))
            elif path == "/api/orders":
                user = self.authenticated_user(required=True)
                with connect_database() as connection:
                    orders = connection.execute(
                        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (user["id"],)
                    ).fetchall()
                    result = []
                    for order in orders:
                        items = connection.execute(
                            "SELECT * FROM order_items WHERE order_id = ?", (order["id"],)
                        ).fetchall()
                        result.append(
                            {
                                "id": order["id"],
                                "email": order["email"],
                                "endereco": json.loads(order["address_json"]),
                                "total": order["total_centavos"] / 100,
                                "criadoEm": order["created_at"],
                                "itens": [
                                    {
                                        "produtoId": item["product_id"],
                                        "nome": item["product_name"],
                                        "imagem": item["product_image"],
                                        "preco": item["unit_price_centavos"] / 100,
                                        "quantidade": item["quantity"],
                                    }
                                    for item in items
                                ],
                            }
                        )
                self.send_json(200, result)
            else:
                raise ApiError(404, "Rota não encontrada.")
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception:
            self.log_error("Falha ao processar GET %s", path)
            self.send_json(500, {"error": "Erro interno ao processar a requisição."})

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            data = self.read_json()
            if path == "/api/auth/register":
                self.register(data)
            elif path == "/api/auth/login":
                self.login(data)
            elif path == "/api/auth/logout":
                self.logout()
            elif path == "/api/auth/forgot-password":
                self.forgot_password(data)
            elif path == "/api/auth/reset-password":
                self.reset_password(data)
            elif path == "/api/orders":
                self.create_order(data)
            elif path == "/api/products":
                self.require_admin()
                product = validate_product(data)
                with connect_database() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO products
                            (nome, descricao, imagem, categoria, plataforma, preco_centavos, estoque)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            product["nome"], product["descricao"], product["imagem"],
                            product["categoria"], product["plataforma"],
                            product["preco_centavos"], product["estoque"],
                        ),
                    )
                    row = connection.execute("SELECT * FROM products WHERE id = ?", (cursor.lastrowid,)).fetchone()
                self.send_json(201, product_json(row))
            else:
                raise ApiError(404, "Rota não encontrada.")
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception:
            self.log_error("Falha ao processar POST %s", path)
            self.send_json(500, {"error": "Erro interno ao processar a requisição."})

    def do_PUT(self):
        self.update_product(replace=True)

    def do_PATCH(self):
        self.update_product(replace=False)

    def update_product(self, replace):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            self.require_admin()
            match = re.fullmatch(r"/api/products/(\d+)", path)
            if not match:
                raise ApiError(404, "Rota não encontrada.")
            product_id = int(match.group(1))
            data = self.read_json()
            if not replace:
                with connect_database() as connection:
                    current = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
                if current is None:
                    raise ApiError(404, "Produto não encontrado.")
                current_data = product_json(current)
                current_data.update(data)
                data = current_data
            product = validate_product(data)
            with connect_database() as connection:
                cursor = connection.execute(
                    """
                    UPDATE products SET nome = ?, descricao = ?, imagem = ?, categoria = ?,
                        plataforma = ?, preco_centavos = ?, estoque = ? WHERE id = ?
                    """,
                    (
                        product["nome"], product["descricao"], product["imagem"],
                        product["categoria"], product["plataforma"],
                        product["preco_centavos"], product["estoque"], product_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise ApiError(404, "Produto não encontrado.")
                row = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            self.send_json(200, product_json(row))
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception:
            self.log_error("Falha ao atualizar produto")
            self.send_json(500, {"error": "Erro interno ao processar a requisição."})

    def do_DELETE(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            self.require_admin()
            match = re.fullmatch(r"/api/products/(\d+)", path)
            if not match:
                raise ApiError(404, "Rota não encontrada.")
            with connect_database() as connection:
                cursor = connection.execute("DELETE FROM products WHERE id = ?", (int(match.group(1)),))
            if cursor.rowcount == 0:
                raise ApiError(404, "Produto não encontrado.")
            self.send_json(200, {"message": "Produto removido."})
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception:
            self.log_error("Falha ao remover produto")
            self.send_json(500, {"error": "Erro interno ao processar a requisição."})

    def register(self, data):
        nome = data.get("nome")
        email = data.get("email")
        cpf = re.sub(r"\D", "", str(data.get("cpf", "")))
        telefone = data.get("telefone")
        password = data.get("senha")
        if not isinstance(nome, str) or len(nome.strip()) < 2:
            raise ApiError(400, "Informe um nome válido.")
        if not isinstance(email, str) or not EMAIL_PATTERN.fullmatch(email.strip()):
            raise ApiError(400, "Informe um e-mail válido.")
        if len(cpf) != 11:
            raise ApiError(400, "O CPF deve conter 11 dígitos.")
        if not isinstance(telefone, str) or not re.fullmatch(r"\(\d{2}\) \d{6}-\d{4}", telefone):
            raise ApiError(400, "Informe um telefone no formato (11) 123456-7890.")
        if not isinstance(password, str) or len(password) < 6:
            raise ApiError(400, "A senha deve ter pelo menos 6 caracteres.")
        salt = secrets.token_bytes(16).hex()
        try:
            with connect_database() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO users (nome, email, cpf, telefone, password_salt, password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        nome.strip(), email.strip().lower(), cpf, telefone,
                        salt, password_digest(password, salt), int(time.time()),
                    ),
                )
                row = connection.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:
            raise ApiError(409, "Já existe uma conta com esse e-mail.")
        self.send_json(201, {"usuario": public_user(row), "message": "Cadastro realizado com sucesso."})

    def login(self, data):
        email = data.get("email")
        password = data.get("senha")
        if not isinstance(email, str) or not isinstance(password, str):
            raise ApiError(400, "Informe e-mail e senha.")
        with connect_database() as connection:
            row = connection.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        if row is None or not hmac.compare_digest(
            password_digest(password, row["password_salt"]), row["password_hash"]
        ):
            raise ApiError(401, "E-mail ou senha incorretos.")
        token = secrets.token_urlsafe(32)
        with connect_database() as connection:
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (hashlib.sha256(token.encode("utf-8")).hexdigest(), row["id"], int(time.time()) + 14 * 86400),
            )
        self.send_json(200, {"token": token, "usuario": public_user(row)})

    def logout(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if token:
            with connect_database() as connection:
                connection.execute(
                    "DELETE FROM sessions WHERE token_hash = ?",
                    (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
                )
        self.send_json(200, {"message": "Sessão encerrada."})

    def forgot_password(self, data):
        email = data.get("email")
        if not isinstance(email, str) or not EMAIL_PATTERN.fullmatch(email.strip()):
            raise ApiError(400, "Informe um e-mail válido.")
        token = secrets.token_urlsafe(32)
        with connect_database() as connection:
            user = connection.execute(
                "SELECT id FROM users WHERE email = ?", (email.strip().lower(),)
            ).fetchone()
            if user:
                connection.execute(
                    "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                    (
                        hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        user["id"],
                        int(time.time()) + 1800,
                    ),
                )

        message = "Se houver uma conta com esse e-mail, as instruções de recuperação foram enviadas."
        if self.email_provider_configured():
            if user:
                self.send_reset_email(email.strip(), token)
            self.send_json(202, {"message": message})
            return
        if os.environ.get("PLAYSHOP_ENV", "development").lower() == "production":
            raise ApiError(503, "Configure o provedor de e-mail para habilitar a recuperação de senha.")
        self.send_json(202, {"message": message, "demoToken": token if user else None})

    def email_provider_configured(self):
        return bool(
            os.environ.get("PLAYSHOP_RESEND_API_KEY")
            or os.environ.get("PLAYSHOP_SMTP_HOST")
        )

    def send_email(self, message, purpose):
        sender = os.environ.get("PLAYSHOP_EMAIL_FROM") or os.environ.get("PLAYSHOP_SMTP_FROM")
        if not sender:
            raise ApiError(503, "Configure PLAYSHOP_EMAIL_FROM para habilitar o envio de e-mails.")
        message["From"] = sender
        resend_api_key = os.environ.get("PLAYSHOP_RESEND_API_KEY")
        if resend_api_key:
            request_body = json.dumps(
                {
                    "from": sender,
                    "to": [message["To"]],
                    "subject": message["Subject"],
                    "text": message.get_content(),
                }
            ).encode("utf-8")
            request = Request(
                "https://api.resend.com/emails",
                data=request_body,
                headers={
                    "Authorization": f"Bearer {resend_api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=10):
                    return
            except HTTPError as error:
                self.log_error("Resend rejeitou o e-mail (%s).", error.code)
                raise ApiError(503, f"Não foi possível enviar o e-mail {purpose}.")
            except (URLError, OSError, ValueError):
                raise ApiError(503, f"Não foi possível enviar o e-mail {purpose}.")

        host = os.environ.get("PLAYSHOP_SMTP_HOST")
        if not host:
            raise ApiError(503, "Configure um provedor de e-mail para habilitar os envios.")
        try:
            port = int(os.environ.get("PLAYSHOP_SMTP_PORT", "587"))
            with smtplib.SMTP(host, port, timeout=10) as smtp:
                smtp.starttls()
                username = os.environ.get("PLAYSHOP_SMTP_USER")
                password = os.environ.get("PLAYSHOP_SMTP_PASSWORD")
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException, ValueError):
            raise ApiError(503, f"Não foi possível enviar o e-mail {purpose}.")

    def send_reset_email(self, recipient, token):
        message = EmailMessage()
        message["Subject"] = "Recuperação de senha - PlayShop"
        message["To"] = recipient
        message.set_content(
            "Use este código na tela de recuperação da PlayShop. "
            "Ele expira em 30 minutos:\n\n" + token
        )
        self.send_email(message, "de recuperação")

    def send_order_confirmation(self, recipient, order_id, total, order_items):
        if not self.email_provider_configured():
            return "Provedor de e-mail não configurado. O pedido foi registrado, mas o e-mail não foi enviado."

        message = EmailMessage()
        message["Subject"] = f"Compra confirmada - Pedido #{order_id} - PlayShop"
        message["To"] = recipient
        item_lines = [
            f"- {product['nome']} | quantidade: {quantity} | "
            f"R$ {product['preco_centavos'] / 100:.2f}"
            for product, quantity in order_items
        ]
        message.set_content(
            f"Olá!\n\nSeu pedido #{order_id} foi confirmado.\n\n"
            f"Produtos:\n{chr(10).join(item_lines)}\n\n"
            f"Total: R$ {total / 100:.2f}\n\nObrigado por comprar na PlayShop!"
        )
        try:
            self.send_email(message, "de confirmação do pedido")
        except ApiError as error:
            self.log_error("Falha ao enviar confirmação do pedido %s: %s", order_id, error.message)
            return error.message
        return None

    def reset_password(self, data):
        token = data.get("token")
        password = data.get("senha")
        if not isinstance(token, str) or not token or not isinstance(password, str) or len(password) < 6:
            raise ApiError(400, "Informe o código de recuperação e uma senha com pelo menos 6 caracteres.")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reset = connection.execute(
                "SELECT user_id FROM password_resets WHERE token_hash = ? AND expires_at > ?",
                (token_hash, int(time.time())),
            ).fetchone()
            if reset is None:
                raise ApiError(400, "Código de recuperação inválido ou expirado.")
            salt = secrets.token_bytes(16).hex()
            connection.execute(
                "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
                (salt, password_digest(password, salt), reset["user_id"]),
            )
            connection.execute("DELETE FROM password_resets WHERE user_id = ?", (reset["user_id"],))
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (reset["user_id"],))
        self.send_json(200, {"message": "Senha alterada. Entre com sua nova senha."})

    def create_order(self, data):
        items = data.get("itens")
        address = data.get("endereco")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "O pedido precisa conter pelo menos um produto.")
        address_fields = ("cep", "rua", "numero", "bairro", "cidade", "estado")
        if not isinstance(address, dict) or any(
            not isinstance(address.get(field), str) or not address[field].strip() for field in address_fields
        ):
            raise ApiError(400, "Informe um endereço de entrega completo.")
        if not re.fullmatch(r"\d{5}-?\d{3}", address["cep"]):
            raise ApiError(400, "Informe um CEP válido.")
        if not re.fullmatch(r"[A-Za-z]{2}", address["estado"]):
            raise ApiError(400, "Informe uma UF válida.")
        user = self.authenticated_user(required=True)
        email = user["email"]
        product_quantities = {}
        for item in items:
            if not isinstance(item, dict):
                raise ApiError(400, "Item inválido no pedido.")
            product_id = item.get("produtoId")
            quantity = item.get("quantidade")
            if (
                isinstance(product_id, bool) or not isinstance(product_id, int) or product_id < 1
                or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1
            ):
                raise ApiError(400, "Produto e quantidade inválidos no pedido.")
            product_quantities[product_id] = product_quantities.get(product_id, 0) + quantity
        try:
            with connect_database() as connection:
                connection.execute("BEGIN IMMEDIATE")
                order_items = []
                total = 0
                for product_id, quantity in product_quantities.items():
                    product = connection.execute(
                        "SELECT * FROM products WHERE id = ?", (product_id,)
                    ).fetchone()
                    if product is None:
                        raise ApiError(404, f"Produto {product_id} não encontrado.")
                    if product["estoque"] < quantity:
                        raise ApiError(409, f"Estoque insuficiente para {product['nome']}.")
                    line_total = product["preco_centavos"] * quantity
                    total += line_total
                    order_items.append((product, quantity))
                cursor = connection.execute(
                    """
                    INSERT INTO orders (user_id, email, address_json, total_centavos, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user["id"] if user else None,
                        email,
                        json.dumps(
                            {key: value.strip() for key, value in address.items() if isinstance(value, str)},
                            ensure_ascii=False,
                        ),
                        total,
                        int(time.time()),
                    ),
                )
                order_id = cursor.lastrowid
                for product, quantity in order_items:
                    connection.execute(
                        "UPDATE products SET estoque = estoque - ? WHERE id = ?",
                        (quantity, product["id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO order_items
                            (order_id, product_id, product_name, product_image, unit_price_centavos, quantity)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            order_id,
                            product["id"],
                            product["nome"],
                            product["imagem"],
                            product["preco_centavos"],
                            quantity,
                        ),
                    )
        except sqlite3.IntegrityError:
            raise ApiError(409, "Não foi possível registrar o pedido.")
        email_warning = self.send_order_confirmation(email, order_id, total, order_items)
        self.send_json(
            201,
            {
                "id": order_id,
                "total": total / 100,
                "status": "confirmado",
                "message": "Pedido registrado com sucesso.",
                "emailEnviado": email_warning is None,
                "avisoEmail": email_warning,
            },
        )


class PlayShopServer(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    initialize_database()
    server = PlayShopServer((HOST, PORT), PlayShopHandler)
    print(f"PlayShop API disponível em http://{HOST}:{PORT}/api")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()
