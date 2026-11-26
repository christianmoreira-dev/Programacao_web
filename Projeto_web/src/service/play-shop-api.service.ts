import { HttpClient, HttpErrorResponse, HttpHeaders } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { Produto } from '../model/Produto';

export interface UsuarioApi {
  id: number;
  nome: string;
  email: string;
}

export interface LoginApiResponse {
  token: string;
  usuario: UsuarioApi;
}

export interface PedidoApiResponse {
  id: number;
  total: number;
  status: string;
  message: string;
  emailEnviado: boolean;
  avisoEmail: string | null;
}

export interface PedidoItemApi {
  produtoId: number | null;
  nome: string;
  imagem: string;
  preco: number;
  quantidade: number;
}

export interface PedidoApi {
  id: number;
  email: string;
  endereco: EnderecoEntrega;
  total: number;
  criadoEm: number;
  itens: PedidoItemApi[];
}

export interface EnderecoEntrega {
  cep: string;
  rua: string;
  numero: string;
  complemento: string;
  bairro: string;
  cidade: string;
  estado: string;
}

@Injectable({ providedIn: 'root' })
export class PlayShopApiService {
  private readonly baseUrl = 'http://localhost:3000/api';

  constructor(private readonly http: HttpClient) {}

  verificarApi(): Promise<{ status: string }> {
    return firstValueFrom(this.http.get<{ status: string }>(`${this.baseUrl}/health`));
  }

  listarProdutos(): Promise<Produto[]> {
    return firstValueFrom(this.http.get<Produto[]>(`${this.baseUrl}/products`));
  }

  obterProduto(id: number): Promise<Produto> {
    return firstValueFrom(this.http.get<Produto>(`${this.baseUrl}/products/${id}`));
  }

  criarProduto(dados: Omit<Produto, 'id'>, adminKey: string): Promise<Produto> {
    return firstValueFrom(
      this.http.post<Produto>(`${this.baseUrl}/products`, dados, { headers: this.adminHeaders(adminKey) }),
    );
  }

  atualizarProduto(id: number, dados: Partial<Omit<Produto, 'id'>>, adminKey: string): Promise<Produto> {
    return firstValueFrom(
      this.http.patch<Produto>(
        `${this.baseUrl}/products/${id}`,
        dados,
        { headers: this.adminHeaders(adminKey) },
      ),
    );
  }

  substituirProduto(id: number, dados: Omit<Produto, 'id'>, adminKey: string): Promise<Produto> {
    return firstValueFrom(
      this.http.put<Produto>(
        `${this.baseUrl}/products/${id}`,
        dados,
        { headers: this.adminHeaders(adminKey) },
      ),
    );
  }

  removerProduto(id: number, adminKey: string): Promise<{ message: string }> {
    return firstValueFrom(
      this.http.delete<{ message: string }>(
        `${this.baseUrl}/products/${id}`,
        { headers: this.adminHeaders(adminKey) },
      ),
    );
  }

  obterUsuario(token: string): Promise<UsuarioApi> {
    return firstValueFrom(this.http.get<UsuarioApi>(`${this.baseUrl}/auth/me`, { headers: this.authHeaders(token) }));
  }

  cadastrar(dados: { nome: string; email: string; cpf: string; telefone: string; senha: string }): Promise<{ message: string }> {
    return firstValueFrom(this.http.post<{ message: string }>(`${this.baseUrl}/auth/register`, dados));
  }

  login(email: string, senha: string): Promise<LoginApiResponse> {
    return firstValueFrom(this.http.post<LoginApiResponse>(`${this.baseUrl}/auth/login`, { email, senha }));
  }

  sair(token: string): Promise<void> {
    return firstValueFrom(
      this.http.post<void>(`${this.baseUrl}/auth/logout`, {}, { headers: this.authHeaders(token) }),
    );
  }

  solicitarRecuperacao(email: string): Promise<{ message: string; demoToken?: string | null }> {
    return firstValueFrom(
      this.http.post<{ message: string; demoToken?: string | null }>(`${this.baseUrl}/auth/forgot-password`, { email }),
    );
  }

  redefinirSenha(token: string, senha: string): Promise<{ message: string }> {
    return firstValueFrom(
      this.http.post<{ message: string }>(`${this.baseUrl}/auth/reset-password`, { token, senha }),
    );
  }

  criarPedido(
    itens: Array<{ produtoId: number; quantidade: number }>,
    endereco: EnderecoEntrega,
    token: string | null,
  ): Promise<PedidoApiResponse> {
    return firstValueFrom(
      this.http.post<PedidoApiResponse>(
        `${this.baseUrl}/orders`,
        { itens, endereco },
        { headers: token ? this.authHeaders(token) : undefined },
      ),
    );
  }

  listarPedidos(token: string): Promise<PedidoApi[]> {
    return firstValueFrom(
      this.http.get<PedidoApi[]>(`${this.baseUrl}/orders`, { headers: this.authHeaders(token) }),
    );
  }

  mensagemErro(error: unknown): string {
    if (error instanceof HttpErrorResponse) {
      if (typeof error.error?.error === 'string') {
        return error.error.error;
      }
      if (error.status === 0) {
        return 'Não foi possível conectar à API. Inicie o backend e tente novamente.';
      }
      return `A API retornou um erro (${error.status}).`;
    }
    return 'Ocorreu um erro inesperado ao comunicar com a API.';
  }

  private authHeaders(token: string): HttpHeaders {
    return new HttpHeaders({ Authorization: `Bearer ${token}` });
  }

  private adminHeaders(adminKey: string): HttpHeaders {
    return new HttpHeaders({ 'X-Admin-Key': adminKey });
  }
}
