import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import MagicMock, patch

import server


class PlayShopApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        server.DATABASE_PATH = Path(self.temporary_directory.name) / "test.db"
        server.initialize_database()
        self.previous_admin_key = os.environ.get("PLAYSHOP_ADMIN_KEY")
        os.environ["PLAYSHOP_ADMIN_KEY"] = "test-admin-key"
        self.previous_smtp_settings = {
            key: os.environ.get(key)
            for key in (
                "PLAYSHOP_SMTP_HOST",
                "PLAYSHOP_SMTP_FROM",
                "PLAYSHOP_SMTP_PORT",
                "PLAYSHOP_SMTP_USER",
                "PLAYSHOP_SMTP_PASSWORD",
                "PLAYSHOP_RESEND_API_KEY",
                "PLAYSHOP_EMAIL_FROM",
            )
        }
        for key in self.previous_smtp_settings:
            os.environ.pop(key, None)
        self.httpd = server.PlayShopServer(("127.0.0.1", 0), server.PlayShopHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        if self.previous_admin_key is None:
            os.environ.pop("PLAYSHOP_ADMIN_KEY", None)
        else:
            os.environ["PLAYSHOP_ADMIN_KEY"] = self.previous_admin_key
        for key, value in self.previous_smtp_settings.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary_directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def create_account_and_login(self):
        status, _ = self.request(
            "POST",
            "/api/auth/register",
            {
                "nome": "Pessoa Teste",
                "email": "teste@example.com",
                "cpf": "12345678901",
                "telefone": "(11) 123456-7890",
                "senha": "senha123",
            },
        )
        self.assertEqual(status, 201)
        status, response = self.request(
            "POST",
            "/api/auth/login",
            {"email": "teste@example.com", "senha": "senha123"},
        )
        self.assertEqual(status, 200)
        return {"Authorization": f"Bearer {response['token']}"}

    def test_product_catalog_and_admin_crud(self):
        status, products = self.request("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertEqual(len(products), 9)

        product = {
            "nome": "Jogo de teste",
            "descricao": "Produto para validar o CRUD.",
            "imagem": "https://example.com/jogo.jpg",
            "categoria": "Teste",
            "plataforma": "PC",
            "preco": 10.5,
            "estoque": 4,
        }
        status, created = self.request(
            "POST", "/api/products", product, {"X-Admin-Key": "test-admin-key"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["preco"], 10.5)

        status, updated = self.request(
            "PATCH",
            f"/api/products/{created['id']}",
            {"estoque": 2},
            {"X-Admin-Key": "test-admin-key"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["estoque"], 2)

        status, _ = self.request(
            "DELETE", f"/api/products/{created['id']}", headers={"X-Admin-Key": "test-admin-key"}
        )
        self.assertEqual(status, 200)
        status, _ = self.request("GET", f"/api/products/{created['id']}")
        self.assertEqual(status, 404)

    def test_order_uses_database_price_and_updates_stock(self):
        headers = self.create_account_and_login()
        order = {
            "itens": [{"produtoId": 1, "quantidade": 2}],
            "endereco": {
                "cep": "01001-000",
                "rua": "Praça da Sé",
                "numero": "1",
                "complemento": "",
                "bairro": "Sé",
                "cidade": "São Paulo",
                "estado": "SP",
            },
        }
        resend_response = MagicMock()
        resend_response.__enter__.return_value = resend_response
        with patch.dict(
            os.environ,
            {
                "PLAYSHOP_RESEND_API_KEY": "re_test_key",
                "PLAYSHOP_EMAIL_FROM": "PlayShop <loja@example.com>",
            },
        ), patch("server.urlopen", return_value=resend_response) as resend_send:
            status, response = self.request("POST", "/api/orders", order, headers)
        self.assertEqual(status, 201)
        self.assertEqual(response["total"], 499.8)
        self.assertTrue(response["emailEnviado"])
        self.assertIsNone(response["avisoEmail"])
        resend_send.assert_called_once()
        resend_request = resend_send.call_args.args[0]
        self.assertEqual(resend_request.full_url, "https://api.resend.com/emails")
        self.assertEqual(resend_request.get_header("Authorization"), "Bearer re_test_key")
        email_payload = json.loads(resend_request.data.decode("utf-8"))
        self.assertEqual(email_payload["from"], "PlayShop <loja@example.com>")
        self.assertEqual(email_payload["to"], ["teste@example.com"])
        self.assertIn("Pedido #", email_payload["subject"])
        self.assertIn("God of War Ragnarök", email_payload["text"])

        status, products = self.request("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertEqual(products[0]["estoque"], 10)

        status, orders = self.request("GET", "/api/orders", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["itens"][0]["quantidade"], 2)
        self.assertEqual(orders[0]["itens"][0]["imagem"], products[0]["imagem"])

    def test_initialize_database_migrates_existing_order_images(self):
        previous_database_path = server.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temporary_directory:
            legacy_database_path = Path(temporary_directory) / "legacy.db"
            connection = sqlite3.connect(legacy_database_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE products (
                        id INTEGER PRIMARY KEY,
                        nome TEXT NOT NULL,
                        descricao TEXT NOT NULL,
                        imagem TEXT NOT NULL,
                        categoria TEXT NOT NULL,
                        plataforma TEXT NOT NULL,
                        preco_centavos INTEGER NOT NULL,
                        estoque INTEGER NOT NULL
                    );
                    INSERT INTO products VALUES
                        (1, 'Jogo legado', 'Descrição', 'https://example.com/capa.jpg',
                         'Teste', 'PC', 1000, 1);
                    CREATE TABLE order_items (
                        id INTEGER PRIMARY KEY,
                        order_id INTEGER NOT NULL,
                        product_id INTEGER,
                        product_name TEXT NOT NULL,
                        unit_price_centavos INTEGER NOT NULL,
                        quantity INTEGER NOT NULL
                    );
                    INSERT INTO order_items VALUES (1, 1, 1, 'Jogo legado', 1000, 1);
                    """
                )
            finally:
                connection.close()
            try:
                server.DATABASE_PATH = legacy_database_path
                server.initialize_database()
                with server.connect_database() as connection:
                    item = connection.execute(
                        "SELECT product_image FROM order_items WHERE id = 1"
                    ).fetchone()
                self.assertEqual(item["product_image"], "https://example.com/capa.jpg")
            finally:
                server.DATABASE_PATH = previous_database_path

    def test_insufficient_stock_does_not_create_order(self):
        headers = self.create_account_and_login()
        order = {
            "itens": [{"produtoId": 1, "quantidade": 13}],
            "endereco": {
                "cep": "01001-000",
                "rua": "Praça da Sé",
                "numero": "1",
                "complemento": "",
                "bairro": "Sé",
                "cidade": "São Paulo",
                "estado": "SP",
            },
        }
        status, response = self.request("POST", "/api/orders", order, headers)
        self.assertEqual(status, 409)
        self.assertIn("Estoque insuficiente", response["error"])
        status, products = self.request("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertEqual(products[0]["estoque"], 12)

    def test_order_requires_authenticated_customer(self):
        order = {
            "itens": [{"produtoId": 1, "quantidade": 1}],
            "endereco": {
                "cep": "01001-000",
                "rua": "Praça da Sé",
                "numero": "1",
                "bairro": "Sé",
                "cidade": "São Paulo",
                "estado": "SP",
            },
        }
        status, response = self.request("POST", "/api/orders", order)
        self.assertEqual(status, 401)
        self.assertIn("Faça login", response["error"])

    def test_password_recovery_changes_password_and_invalidates_sessions(self):
        headers = self.create_account_and_login()
        status, response = self.request(
            "POST", "/api/auth/forgot-password", {"email": "teste@example.com"}
        )
        self.assertEqual(status, 202)
        self.assertTrue(response["demoToken"])

        status, _ = self.request(
            "POST",
            "/api/auth/reset-password",
            {"token": response["demoToken"], "senha": "novaSenha123"},
        )
        self.assertEqual(status, 200)
        status, expired_session = self.request("GET", "/api/auth/me", headers=headers)
        self.assertEqual(status, 401)
        self.assertIn("expirou", expired_session["error"])

        status, _ = self.request(
            "POST",
            "/api/auth/login",
            {"email": "teste@example.com", "senha": "novaSenha123"},
        )
        self.assertEqual(status, 200)

    def test_password_recovery_sends_reset_token_using_resend(self):
        self.create_account_and_login()
        resend_response = MagicMock()
        resend_response.__enter__.return_value = resend_response
        with patch.dict(
            os.environ,
            {
                "PLAYSHOP_RESEND_API_KEY": "re_test_key",
                "PLAYSHOP_EMAIL_FROM": "PlayShop <loja@example.com>",
            },
        ), patch("server.urlopen", return_value=resend_response) as resend_send:
            status, response = self.request(
                "POST", "/api/auth/forgot-password", {"email": "teste@example.com"}
            )

        self.assertEqual(status, 202)
        self.assertNotIn("demoToken", response)
        resend_send.assert_called_once()
        resend_request = resend_send.call_args.args[0]
        email_payload = json.loads(resend_request.data.decode("utf-8"))
        self.assertEqual(email_payload["to"], ["teste@example.com"])
        self.assertIn("Recuperação de senha", email_payload["subject"])
        self.assertIn("expira em 30 minutos", email_payload["text"])


if __name__ == "__main__":
    unittest.main()
