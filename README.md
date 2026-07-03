# perfecthub-mcp

Servidor MCP (Model Context Protocol) que expõe a API v2 do PerfectHub
(WhatsApp Business) como *tools* para o Hermes Agent — envio de mensagens,
gestão de contatos/grupos, templates e OTP via linguagem natural.

Transporte: `stdio`. Sem porta exposta, sem domínio próprio — o Hermes
sobe o processo localmente, conversa via stdin/stdout, e encerra depois.

## Escopo (v1)

25 tools, cobrindo leitura + escrita não-destrutiva:

| Categoria | Tools |
|---|---|
| Mensagens | `send_text_message`, `send_template_message`, `send_media_message`, `send_interactive_message`, `send_cta_message`, `list_messages`, `get_message` |
| Contatos | `list_contacts`, `create_contact`, `get_contact`, `update_contact` |
| Grupos | `list_groups`, `create_group`, `add_contacts_to_group` |
| Templates | `list_templates`, `get_template` |
| Autenticação/OTP | `list_auth_templates`, `send_otp`, `verify_otp`, `resend_otp`, `check_otp_status` |
| Origens/Status | `list_sources`, `list_statuses` (leitura, necessário para `create_contact`) |
| Conta | `get_account_info`, `get_usage_stats`, `get_plan_limits` |

**Fora do escopo (deliberado):** delete de contatos/grupos/sources/statuses,
batch delete, remoção de contatos de grupo, e `/templates/sync` — todos
irreversíveis ou com efeito colateral externo (Meta Business Manager).

## Setup local (teste antes de subir pro GitHub)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip install -r requirements.txt

cp .env.example .env
# edite .env e preencha PERFECTHUB_API_TOKEN com um token de tenant de teste

# rodar manualmente (stdio — vai ficar esperando input, Ctrl+C para sair)
PERFECTHUB_API_TOKEN=seu_token .venv/bin/python server.py

# ou testar interativamente com o MCP Inspector
npx @modelcontextprotocol/inspector .venv/bin/python server.py
```

Verificação rápida de sintaxe:

```bash
python -m py_compile server.py
```

## Configuração no Hermes (uma vez por perfil)

Cada perfil (12WEB Marketing, PerfectIA, ItPet, Pessoal) tem seu próprio
tenant/token no PerfectHub. Instale este MCP **uma vez por perfil**, com
o token daquele tenant no campo Environment:

```
NAME: perfecthub-whatsapp
TRANSPORT: stdio
Install from: https://github.com/12webmarketing/mcp-perfecthub.git @ main
Bootstrap commands:
  python -m venv .venv
  .venv/bin/pip install -r requirements.txt
Runs: ${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/server.py
ENVIRONMENT:
  PERFECTHUB_API_TOKEN=<token do tenant deste perfil>
  PERFECTHUB_BASE_URL=https://perfecthub.com.br/api/v2
```

O servidor é stateless quanto a tenant: só lê `PERFECTHUB_API_TOKEN` na
inicialização e usa esse token em todas as chamadas daquela instância. Não
há lógica de roteamento por tenant no código — a separação acontece
inteiramente pela variável de ambiente configurada em cada instalação.

## Variáveis de ambiente

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `PERFECTHUB_API_TOKEN` | Sim | — | Bearer token do tenant. Nunca commitar. |
| `PERFECTHUB_BASE_URL` | Não | `https://perfecthub.com.br/api/v2` | Base URL da API v2. |

## Tratamento de erros

A API PerfectHub responde sempre no formato:

```json
{ "success": true, "data": {...}, "meta": {...} }
```
ou
```json
{ "success": false, "error": { "code": "...", "message": "...", "details": {...} }, "meta": {...} }
```

Cada tool trata `success: false` internamente e retorna:

```json
{ "success": false, "error_code": "...", "message": "..." }
```

de forma legível para o agente, em vez de propagar o JSON cru da API.

## Rate limits

60 chamadas/min por token por padrão (configurável por plano — ver a tool
`get_plan_limits` → `api_calls_per_minute`). Erros 429 retornam
`error_code: "RATE_LIMIT_EXCEEDED"`.

## Publicando no GitHub

```bash
cd mcp-perfecthub
git init
git add .
git commit -m "Initial MCP server for PerfectHub WhatsApp API v2"
git branch -M main
git remote add origin https://github.com/12webmarketing/mcp-perfecthub.git
git push -u origin main
```

Se o repositório remoto já tiver conteúdo (README inicial, licença, etc.),
faça `git pull --rebase origin main` antes do push, ou `git push -u origin main --force`
apenas se tiver certeza de que quer sobrescrever o histórico remoto.

## Segurança

- `PERFECTHUB_API_TOKEN` nunca é logado em texto plano.
- Merge fields (`@{contact_first_name}` etc.) são resolvidos pelo próprio
  PerfectHub — o servidor MCP não precisa parsear isso.
- Mensagens de template podem ser enviadas fora da janela de 24h; texto e
  mídia não podem — a API valida isso e o erro é repassado ao agente.
- Nenhuma tool desta v1 executa exclusão em massa ou ação irreversível sem
  supervisão explícita.
