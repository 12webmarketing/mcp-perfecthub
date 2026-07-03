#!/usr/bin/env python3
"""
MCP Server para PerfectHub WhatsApp API v2.

Expõe um subconjunto seguro (leitura + escrita não-destrutiva) da API v2 do
PerfectHub (app.perfecthub.com.br) como tools MCP, para uso por agentes do
Hermes via transporte stdio.

Autenticação: variável de ambiente PERFECTHUB_API_TOKEN (Bearer token),
lida uma única vez na inicialização do processo. O servidor é stateless
quanto a tenant — cada instância/processo atende a um único tenant/token.

Endpoints destrutivos (DELETE de contatos/grupos/sources/statuses, batch
delete, remoção de contatos de grupo, e /templates/sync) foram
deliberadamente deixados de fora desta v1.
"""

import json
import os
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

mcp = FastMCP("perfecthub_mcp")

API_BASE_URL = os.environ.get(
    "PERFECTHUB_BASE_URL", "https://perfecthub.com.br/api/v2"
).rstrip("/")
API_TOKEN = os.environ.get("PERFECTHUB_API_TOKEN", "").strip()

PHONE_PATTERN = r"^[0-9+\-\s()]{10,20}$"

# ---------------------------------------------------------------------------
# Cliente HTTP compartilhado / tratamento de erros
# ---------------------------------------------------------------------------


class PerfectHubAPIError(Exception):
    """Erro de negócio retornado pela API PerfectHub (success: false)."""

    def __init__(
        self, code: str, message: str, details: Optional[dict], status_code: int
    ):
        self.code = code
        self.message = message
        self.details = details
        self.status_code = status_code
        super().__init__(f"{code}: {message}")


def _clean(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Remove chaves com valor None de um dict de query params ou body."""
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


async def _request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Função central para todas as chamadas à API PerfectHub.

    Lê o token de PERFECTHUB_API_TOKEN, monta a URL a partir de
    PERFECTHUB_BASE_URL, trata o envelope de resposta padrão
    ({success, data, error, meta}) e levanta PerfectHubAPIError em caso de
    success=false, para que cada tool possa formatar a mensagem de forma
    legível pro agente.
    """
    if not API_TOKEN:
        raise RuntimeError(
            "PERFECTHUB_API_TOKEN não está configurado. Defina essa variável "
            "de ambiente com o token do tenant antes de iniciar o servidor."
        )

    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method,
            f"{API_BASE_URL}{path}",
            headers=headers,
            params=_clean(params),
            json=_clean(json_body) if json_body is not None else None,
        )

    try:
        payload = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError(
            f"Resposta não-JSON da API PerfectHub (status {response.status_code})."
        )

    if payload.get("success") is False:
        err = payload.get("error") or {}
        raise PerfectHubAPIError(
            code=err.get("code", "UNKNOWN_ERROR"),
            message=err.get("message", "Erro desconhecido retornado pela API."),
            details=err.get("details"),
            status_code=response.status_code,
        )

    if not payload.get("success") and response.status_code >= 400:
        response.raise_for_status()

    return payload


def _error_json(e: Exception) -> str:
    """Formata qualquer exceção capturada numa tool como JSON legível."""
    if isinstance(e, PerfectHubAPIError):
        out: Dict[str, Any] = {
            "success": False,
            "error_code": e.code,
            "message": e.message,
        }
        if e.details:
            out["details"] = e.details
        return json.dumps(out, ensure_ascii=False, indent=2)
    if isinstance(e, httpx.TimeoutException):
        return json.dumps(
            {
                "success": False,
                "error_code": "TIMEOUT",
                "message": "A requisição para a API do PerfectHub expirou (30s). Tente novamente.",
            },
            ensure_ascii=False,
        )
    if isinstance(e, httpx.HTTPStatusError):
        return json.dumps(
            {
                "success": False,
                "error_code": "HTTP_ERROR",
                "message": f"Erro HTTP {e.response.status_code} ao chamar a API PerfectHub.",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {"success": False, "error_code": "UNEXPECTED_ERROR", "message": str(e)},
        ensure_ascii=False,
    )


def _ok(data: Any, meta: Optional[dict] = None) -> str:
    out: Dict[str, Any] = {"success": True, "data": data}
    if meta:
        out["meta"] = meta
    return json.dumps(out, ensure_ascii=False, indent=2, default=str)


def _sort_desc(field: str, description_extra: str = "") -> str:
    return (
        f"Ordenação. Prefixe com '-' para descendente (ex: '-created_at'). "
        f"Campos permitidos: {field}.{description_extra}"
    )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ContactType(str, Enum):
    LEAD = "lead"
    CUSTOMER = "customer"
    GUEST = "guest"


class MediaType(str, Enum):
    IMAGE = "image"
    DOCUMENT = "document"
    VIDEO = "video"
    AUDIO = "audio"


class InteractiveType(str, Enum):
    BUTTON = "button"
    LIST = "list"


# ===========================================================================
# MENSAGENS
# ===========================================================================


class SendTextMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    phone: str = Field(
        ...,
        description="Número de telefone no formato internacional, ex: '+5511975844549'.",
        pattern=PHONE_PATTERN,
    )
    message: str = Field(
        ...,
        description=(
            "Texto da mensagem (máx 4096 caracteres). Suporta merge fields "
            "resolvidos pelo próprio PerfectHub: @{contact_first_name}, "
            "@{contact_last_name}, @{contact_email}, @{contact_phone}."
        ),
        min_length=1,
        max_length=4096,
    )
    contact_id: Optional[int] = Field(
        default=None, description="ID interno do contato, se já conhecido."
    )


@mcp.tool(
    name="send_text_message",
    annotations={
        "title": "Enviar Mensagem de Texto (WhatsApp)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_text_message(params: SendTextMessageInput) -> str:
    """Envia uma mensagem de texto simples via WhatsApp.

    Sujeita à janela de atendimento de 24h do WhatsApp (só funciona se o
    contato já interagiu nas últimas 24h). Para mensagens fora dessa janela,
    use send_template_message. Cria o contato automaticamente se o telefone
    for novo (requer status/origem/responsável padrão configurados na conta).

    Args:
        params (SendTextMessageInput): phone, message, contact_id (opcional).

    Returns:
        str: JSON com {success, data: {message_id, contact_id, phone, message,
        status, sent_at, chat_id, chat_message_id}} ou {success: false,
        error_code, message} em caso de erro (ex: CONTACT_OPTED_OUT,
        RATE_LIMIT_EXCEEDED, janela de 24h expirada).
    """
    try:
        payload = await _request(
            "POST",
            "/messages/text",
            json_body={
                "phone": params.phone,
                "message": params.message,
                "contact_id": params.contact_id,
            },
        )
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class SendTemplateMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    phone: str = Field(..., description="Número de telefone internacional.", pattern=PHONE_PATTERN)
    template_name: str = Field(
        ..., description="Nome do template APROVADO (ver list_templates)."
    )
    language: str = Field(
        ..., description="Código de idioma do template, ex: 'en', 'pt_BR'.", max_length=10
    )
    contact_id: Optional[int] = Field(default=None, description="ID interno do contato.")
    header_image_url: Optional[str] = Field(
        default=None, description="URL pública de imagem para o header (jpg/png/webp, máx 5MB)."
    )
    header_video_url: Optional[str] = Field(
        default=None, description="URL pública de vídeo para o header (mp4, máx 16MB)."
    )
    header_document_url: Optional[str] = Field(
        default=None, description="URL pública de documento para o header (máx 100MB)."
    )
    header_document_name: Optional[str] = Field(
        default=None, description="Nome de exibição do documento do header."
    )
    header_field_1: Optional[str] = Field(
        default=None, description="Parâmetro de texto do header, se o template usar."
    )
    field_1: Optional[str] = Field(default=None, description="Parâmetro 1 do corpo do template.")
    field_2: Optional[str] = Field(default=None, description="Parâmetro 2 do corpo do template.")
    field_3: Optional[str] = Field(default=None, description="Parâmetro 3 do corpo do template.")
    field_4: Optional[str] = Field(default=None, description="Parâmetro 4 do corpo do template.")
    field_5: Optional[str] = Field(default=None, description="Parâmetro 5 do corpo do template.")
    field_6: Optional[str] = Field(default=None, description="Parâmetro 6 do corpo do template.")
    field_7: Optional[str] = Field(default=None, description="Parâmetro 7 do corpo do template.")
    field_8: Optional[str] = Field(default=None, description="Parâmetro 8 do corpo do template.")
    field_9: Optional[str] = Field(default=None, description="Parâmetro 9 do corpo do template.")
    field_10: Optional[str] = Field(default=None, description="Parâmetro 10 do corpo do template.")
    button_0: Optional[str] = Field(default=None, description="Valor dinâmico do botão 0, se aplicável.")
    button_1: Optional[str] = Field(default=None, description="Valor dinâmico do botão 1, se aplicável.")
    button_2: Optional[str] = Field(default=None, description="Valor dinâmico do botão 2, se aplicável.")
    auto_generate_otp: Optional[bool] = Field(
        default=None, description="Se true, gera automaticamente um código OTP para templates de autenticação."
    )
    otp_field_number: Optional[int] = Field(
        default=None, description="Número do campo do corpo onde o OTP deve ser inserido (padrão 1)."
    )


@mcp.tool(
    name="send_template_message",
    annotations={
        "title": "Enviar Mensagem de Template (WhatsApp)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_template_message(params: SendTemplateMessageInput) -> str:
    """Envia uma mensagem de template pré-aprovado via WhatsApp.

    Diferente de send_text_message, funciona a qualquer momento,
    independente da janela de 24h — ideal para reengajamento ou primeiro
    contato. Use list_templates ou get_template para confirmar o nome exato,
    idioma e quantidade de parâmetros do template antes de enviar. Esta
    versão da tool suporta apenas mídia de header via URL pública (não
    upload direto de arquivo).

    Args:
        params (SendTemplateMessageInput): phone, template_name, language e
            campos opcionais de header/corpo/botões.

    Returns:
        str: JSON com {success, data: {message_id, contact_id, phone,
        template_name, language, status, sent_at, chat_id,
        chat_message_id}} ou erro (ex: 404 se template não existir/não
        aprovado, 422 se faltar parâmetro obrigatório do template).
    """
    try:
        query = params.model_dump(exclude_none=True)
        payload = await _request("POST", "/messages/template", params=query)
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class SendMediaMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    phone: str = Field(..., description="Número de telefone internacional.", pattern=PHONE_PATTERN)
    media_type: MediaType = Field(..., description="Tipo de mídia: image, document, video ou audio.")
    media_url: str = Field(
        ...,
        description=(
            "URL pública do arquivo de mídia. Limites por tipo: image "
            "jpg/jpeg/png/webp até 5MB; document pdf/doc/docx/xls/xlsx/ppt/"
            "pptx/txt/csv até 100MB; video mp4/3gp até 16MB; audio "
            "mp3/ogg/amr/aac/opus até 16MB (pode variar por plano)."
        ),
    )
    caption: Optional[str] = Field(
        default=None, description="Legenda (não aplicável para audio).", max_length=1024
    )
    filename: Optional[str] = Field(default=None, description="Nome de exibição do arquivo.", max_length=255)
    contact_id: Optional[int] = Field(default=None, description="ID interno do contato.")


@mcp.tool(
    name="send_media_message",
    annotations={
        "title": "Enviar Mídia (WhatsApp)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_media_message(params: SendMediaMessageInput) -> str:
    """Envia imagem, vídeo, documento ou áudio via WhatsApp a partir de uma URL pública.

    Sujeita à janela de 24h de atendimento (exceto quando enviada dentro de
    um fluxo de template). Esta tool aceita apenas media_url (upload direto
    de bytes de arquivo não é suportado nesta versão).

    Args:
        params (SendMediaMessageInput): phone, media_type, media_url,
            caption/filename/contact_id opcionais.

    Returns:
        str: JSON com {success, data: {message_id, contact_id, phone,
        media_type, media_url, caption, filename, status, sent_at, chat_id,
        chat_message_id}} ou erro (ex: 422 INVALID_MEDIA_TYPE, extensão ou
        tamanho não suportado).
    """
    try:
        query = {
            "phone": params.phone,
            "media_type": params.media_type.value,
            "media_url": params.media_url,
            "caption": params.caption,
            "filename": params.filename,
            "contact_id": params.contact_id,
        }
        payload = await _request("POST", "/messages/media", params=query)
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class InteractiveButton(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    id: str = Field(..., description="Identificador do botão, retornado quando o usuário clicar.")
    title: str = Field(..., description="Texto do botão (máx 20 caracteres).", max_length=20)


class InteractiveListRow(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    id: str = Field(..., description="Identificador da opção da lista.")
    title: str = Field(..., description="Título da opção (máx 24 caracteres).", max_length=24)
    description: Optional[str] = Field(
        default=None, description="Descrição da opção (máx 72 caracteres).", max_length=72
    )


class InteractiveListSection(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(..., description="Título da seção da lista.")
    rows: List[InteractiveListRow] = Field(
        ..., description="Opções dessa seção.", min_length=1
    )


class SendInteractiveMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    phone: str = Field(..., description="Número de telefone internacional.", pattern=PHONE_PATTERN)
    type: InteractiveType = Field(..., description="'button' (até 3 botões de resposta) ou 'list' (menu de opções).")
    body_text: str = Field(..., description="Texto principal da mensagem.", max_length=1024)
    header_text: Optional[str] = Field(default=None, description="Texto do cabeçalho.", max_length=60)
    footer_text: Optional[str] = Field(default=None, description="Texto do rodapé.", max_length=60)
    contact_id: Optional[int] = Field(default=None, description="ID interno do contato.")
    buttons: Optional[List[InteractiveButton]] = Field(
        default=None,
        description="Obrigatório quando type='button'. Máximo 3 botões, cada um com id e title.",
        max_length=3,
    )
    list_button_text: Optional[str] = Field(
        default=None, description="Obrigatório quando type='list'. Texto do botão que abre o menu (máx 20 caracteres).", max_length=20
    )
    sections: Optional[List[InteractiveListSection]] = Field(
        default=None,
        description="Obrigatório quando type='list'. Até 10 seções, cada uma com title e rows.",
        max_length=10,
    )


@mcp.tool(
    name="send_interactive_message",
    annotations={
        "title": "Enviar Mensagem Interativa (Botões ou Lista)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_interactive_message(params: SendInteractiveMessageInput) -> str:
    """Envia uma mensagem interativa com botões de resposta rápida ou menu de lista.

    Para type='button', preencha 'buttons' (até 3, cada um com id/title).
    Para type='list', preencha 'list_button_text' e 'sections' (até 10
    seções, cada uma com até várias rows de id/title/description).

    Args:
        params (SendInteractiveMessageInput): phone, type, body_text, e os
            campos específicos do tipo escolhido (buttons OU
            list_button_text+sections).

    Returns:
        str: JSON com {success, data: {message_id, contact_id, phone, type,
        status, sent_at, chat_id, chat_message_id}} ou erro de validação se
        faltar buttons/sections coerente com o type.
    """
    try:
        if params.type == InteractiveType.BUTTON and not params.buttons:
            return _error_json(
                ValueError("Para type='button', o campo 'buttons' é obrigatório (1 a 3 botões).")
            )
        if params.type == InteractiveType.LIST and not (params.list_button_text and params.sections):
            return _error_json(
                ValueError(
                    "Para type='list', os campos 'list_button_text' e 'sections' são obrigatórios."
                )
            )

        body: Dict[str, Any] = {
            "phone": params.phone,
            "type": params.type.value,
            "body_text": params.body_text,
            "header_text": params.header_text,
            "footer_text": params.footer_text,
            "contact_id": params.contact_id,
        }
        if params.type == InteractiveType.BUTTON:
            body["buttons"] = [b.model_dump() for b in params.buttons]
        else:
            body["list_button_text"] = params.list_button_text
            body["sections"] = [s.model_dump(exclude_none=True) for s in params.sections]

        payload = await _request("POST", "/messages/interactive", json_body=body)
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class SendCtaMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    phone: str = Field(..., description="Número de telefone internacional.", pattern=PHONE_PATTERN)
    message: str = Field(..., description="Corpo da mensagem.", max_length=1024)
    button_text: str = Field(..., description="Texto do botão (máx 25 caracteres).", max_length=25)
    button_url: str = Field(..., description="URL que o botão abre ao ser tocado (máx 2048 caracteres).", max_length=2048)
    header_text: Optional[str] = Field(default=None, description="Texto do cabeçalho.", max_length=60)
    footer_text: Optional[str] = Field(default=None, description="Texto do rodapé.", max_length=60)
    contact_id: Optional[int] = Field(default=None, description="ID interno do contato.")


@mcp.tool(
    name="send_cta_message",
    annotations={
        "title": "Enviar Mensagem com Botão de Link (CTA)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_cta_message(params: SendCtaMessageInput) -> str:
    """Envia uma mensagem com um botão de URL verdadeiro (abre link ao tocar).

    Diferente dos botões de send_interactive_message (que só retornam um id
    de resposta), o botão desta mensagem abre diretamente uma URL externa.
    Útil para links de pagamento, agendamento ou páginas de produto.

    Args:
        params (SendCtaMessageInput): phone, message, button_text, button_url,
            header_text/footer_text/contact_id opcionais.

    Returns:
        str: JSON com {success, data: {message_id, contact_id, phone,
        button_text, button_url, status, sent_at, chat_id,
        chat_message_id}} ou erro de validação/envio.
    """
    try:
        body = {
            "phone": params.phone,
            "message": params.message,
            "button_text": params.button_text,
            "button_url": params.button_url,
            "header_text": params.header_text,
            "footer_text": params.footer_text,
            "contact_id": params.contact_id,
        }
        payload = await _request("POST", "/messages/cta", json_body=body)
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class ListMessagesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    page: int = Field(default=1, ge=1, description="Número da página.")
    per_page: int = Field(default=15, ge=1, le=100, description="Itens por página (máx 100).")
    search: Optional[str] = Field(default=None, description="Busca no texto da mensagem.")
    type: Optional[str] = Field(
        default=None,
        description="Filtrar por tipo: text, template, image, video, document, audio, interactive.",
    )
    status: Optional[str] = Field(
        default=None, description="Filtrar por status: sent, delivered, read, failed."
    )
    interaction_id: Optional[int] = Field(default=None, description="Filtrar por ID de interação/conversa.")
    direction: Optional[str] = Field(default=None, description="Filtrar por direção: inbound ou outbound.")
    sort: Optional[str] = Field(
        default=None, description="Ordenação por created_at ou updated_at (padrão '-created_at')."
    )


@mcp.tool(
    name="list_messages",
    annotations={
        "title": "Listar Histórico de Mensagens",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_messages(params: ListMessagesInput) -> str:
    """Lista o histórico de mensagens WhatsApp, mais recentes primeiro, com filtros.

    Args:
        params (ListMessagesInput): paginação, busca por texto e filtros de
            tipo/status/interação/direção.

    Returns:
        str: JSON com {success, data: [{id, interaction_id, sender_id, type,
        message, status, message_id, sent_at, is_read}, ...]} e meta de
        paginação (total, count, per_page, current_page, total_pages,
        has_more).
    """
    try:
        query = {
            "page": params.page,
            "per_page": params.per_page,
            "search": params.search,
            "filter[type]": params.type,
            "filter[status]": params.status,
            "filter[interaction_id]": params.interaction_id,
            "filter[direction]": params.direction,
            "sort": params.sort,
        }
        payload = await _request("GET", "/messages", params=query)
        return _ok(payload.get("data"), meta=payload.get("meta"))
    except Exception as e:
        return _error_json(e)


class GetMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int = Field(..., description="ID interno do registro de mensagem (não o message_id do WhatsApp).")


@mcp.tool(
    name="get_message",
    annotations={
        "title": "Detalhar Mensagem",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_message(params: GetMessageInput) -> str:
    """Retorna os detalhes completos de uma mensagem pelo ID interno.

    Inclui campos extras em relação ao list_messages: status_message,
    ref_message_id, url, staff_id.

    Args:
        params (GetMessageInput): message_id (ID interno).

    Returns:
        str: JSON com {success, data: {...}} ou erro 404 NOT_FOUND se o ID
        não existir.
    """
    try:
        payload = await _request("GET", f"/messages/{params.message_id}")
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


# ===========================================================================
# CONTATOS
# ===========================================================================


class ListContactsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    page: int = Field(default=1, ge=1, description="Número da página.")
    per_page: int = Field(default=15, ge=1, le=100, description="Itens por página (máx 100).")
    search: Optional[str] = Field(
        default=None, description="Busca parcial em firstname, lastname, email, phone, company."
    )
    type: Optional[ContactType] = Field(default=None, description="Filtrar por tipo de contato.")
    status_id: Optional[int] = Field(default=None, description="Filtrar por ID de status (ver list_statuses).")
    source_id: Optional[int] = Field(default=None, description="Filtrar por ID de origem (ver list_sources).")
    assigned_id: Optional[int] = Field(default=None, description="Filtrar por ID do responsável atribuído.")
    group_id: Optional[int] = Field(default=None, description="Filtrar por ID de grupo (ver list_groups).")
    include: Optional[str] = Field(
        default=None, description="Relacionamentos a incluir, separados por vírgula: groups, source, status."
    )
    sort: Optional[str] = Field(
        default=None,
        description="Ordenação. Prefixe com '-' para descendente. Campos: id, firstname, lastname, created_at, updated_at.",
    )


@mcp.tool(
    name="list_contacts",
    annotations={
        "title": "Listar Contatos",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_contacts(params: ListContactsInput) -> str:
    """Lista contatos com paginação, busca e filtros.

    Args:
        params (ListContactsInput): paginação, busca, filtros por tipo/
            status/origem/responsável/grupo, relacionamentos a incluir.

    Returns:
        str: JSON com {success, data: [{id, firstname, lastname, company,
        type, email, phone, status_id, source_id, ...}, ...]} e meta de
        paginação.
    """
    try:
        query = {
            "page": params.page,
            "per_page": params.per_page,
            "search": params.search,
            "filter[type]": params.type.value if params.type else None,
            "filter[status_id]": params.status_id,
            "filter[source_id]": params.source_id,
            "filter[assigned_id]": params.assigned_id,
            "filter[group_id]": params.group_id,
            "include": params.include,
            "sort": params.sort,
        }
        payload = await _request("GET", "/contacts", params=query)
        return _ok(payload.get("data"), meta=payload.get("meta"))
    except Exception as e:
        return _error_json(e)


class CreateContactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    firstname: str = Field(..., description="Primeiro nome.", max_length=255)
    lastname: str = Field(..., description="Sobrenome.", max_length=255)
    phone: str = Field(
        ..., description="Telefone (único por tenant).", max_length=20
    )
    type: ContactType = Field(..., description="Tipo do contato: lead, customer ou guest.")
    source_id: int = Field(..., description="ID de origem, deve existir (ver list_sources).")
    status_id: int = Field(..., description="ID de status, deve existir (ver list_statuses).")
    email: Optional[str] = Field(default=None, description="E-mail (único por tenant).", max_length=191)
    company: Optional[str] = Field(default=None, description="Empresa.", max_length=255)
    description: Optional[str] = Field(default=None, description="Notas/descrição livre.")
    city: Optional[str] = Field(default=None, max_length=255, description="Cidade.")
    state: Optional[str] = Field(default=None, max_length=255, description="Estado.")
    zip: Optional[str] = Field(default=None, max_length=20, description="CEP.")
    address: Optional[str] = Field(default=None, max_length=500, description="Endereço.")
    country_id: Optional[int] = Field(default=None, description="ID do país.")
    assigned_id: Optional[int] = Field(default=None, description="ID do responsável atribuído.")
    groups: Optional[List[int]] = Field(
        default=None, description="Lista de IDs de grupos aos quais adicionar o contato."
    )


@mcp.tool(
    name="create_contact",
    annotations={
        "title": "Criar Contato",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_contact(params: CreateContactInput) -> str:
    """Cria um novo contato.

    IMPORTANTE: source_id e status_id são obrigatórios e devem existir no
    tenant. Use list_sources e list_statuses antes de chamar esta tool caso
    não saiba os IDs válidos.

    Args:
        params (CreateContactInput): campos obrigatórios (firstname,
            lastname, phone, type, source_id, status_id) e opcionais.

    Returns:
        str: JSON com {success, data: {id, firstname, lastname, ...}} ou
        erro 422 VALIDATION_ERROR / 403 FEATURE_LIMIT_EXCEEDED se o limite
        de contatos do plano foi atingido.
    """
    try:
        body = params.model_dump(exclude_none=True)
        body["type"] = params.type.value
        payload = await _request("POST", "/contacts", json_body=body)
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class GetContactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: int = Field(..., description="ID do contato.")
    include: Optional[str] = Field(
        default=None, description="Relacionamentos a incluir, separados por vírgula: groups, source, status."
    )


@mcp.tool(
    name="get_contact",
    annotations={
        "title": "Obter Contato",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_contact(params: GetContactInput) -> str:
    """Retorna os detalhes de um contato pelo ID.

    Args:
        params (GetContactInput): contact_id, include opcional.

    Returns:
        str: JSON com {success, data: {...}} ou erro 404 NOT_FOUND.
    """
    try:
        payload = await _request(
            "GET", f"/contacts/{params.contact_id}", params={"include": params.include}
        )
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class UpdateContactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    contact_id: int = Field(..., description="ID do contato a atualizar.")
    firstname: Optional[str] = Field(default=None, max_length=255, description="Primeiro nome.")
    lastname: Optional[str] = Field(default=None, max_length=255, description="Sobrenome.")
    phone: Optional[str] = Field(default=None, max_length=20, description="Telefone.")
    type: Optional[ContactType] = Field(default=None, description="Tipo do contato.")
    source_id: Optional[int] = Field(default=None, description="ID de origem.")
    status_id: Optional[int] = Field(default=None, description="ID de status.")
    email: Optional[str] = Field(default=None, max_length=191, description="E-mail.")
    company: Optional[str] = Field(default=None, max_length=255, description="Empresa.")
    description: Optional[str] = Field(default=None, description="Notas/descrição.")
    city: Optional[str] = Field(default=None, max_length=255, description="Cidade.")
    state: Optional[str] = Field(default=None, max_length=255, description="Estado.")
    zip: Optional[str] = Field(default=None, max_length=20, description="CEP.")
    address: Optional[str] = Field(default=None, max_length=500, description="Endereço.")
    country_id: Optional[int] = Field(default=None, description="ID do país.")
    assigned_id: Optional[int] = Field(default=None, description="ID do responsável atribuído.")
    groups: Optional[List[int]] = Field(
        default=None,
        description="Lista de IDs de grupos. Se fornecido, SUBSTITUI totalmente os grupos existentes do contato.",
    )


@mcp.tool(
    name="update_contact",
    annotations={
        "title": "Atualizar Contato",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def update_contact(params: UpdateContactInput) -> str:
    """Atualiza parcialmente um contato existente (PATCH).

    Todos os campos são opcionais — apenas os enviados são alterados. Atenção:
    se 'groups' for enviado, ele SUBSTITUI totalmente a lista de grupos atual
    do contato (não é um merge/adição).

    Args:
        params (UpdateContactInput): contact_id + campos a atualizar.

    Returns:
        str: JSON com {success, data: {...}} ou erro 404 NOT_FOUND / 422
        VALIDATION_ERROR.
    """
    try:
        body = params.model_dump(exclude={"contact_id"}, exclude_none=True)
        if params.type is not None:
            body["type"] = params.type.value
        payload = await _request("PATCH", f"/contacts/{params.contact_id}", json_body=body)
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


# ===========================================================================
# GRUPOS
# ===========================================================================


class ListGroupsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    page: int = Field(default=1, ge=1, description="Número da página.")
    per_page: int = Field(default=15, ge=1, le=100, description="Itens por página (máx 100).")
    search: Optional[str] = Field(default=None, description="Busca por nome do grupo.")
    sort: Optional[str] = Field(
        default=None, description="Ordenação por name, created_at ou updated_at."
    )


@mcp.tool(
    name="list_groups",
    annotations={
        "title": "Listar Grupos",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_groups(params: ListGroupsInput) -> str:
    """Lista grupos de contatos com paginação, busca e ordenação.

    Args:
        params (ListGroupsInput): paginação, busca, ordenação.

    Returns:
        str: JSON com {success, data: [{id, name, created_at, updated_at}, ...]}
        e meta de paginação.
    """
    try:
        query = {
            "page": params.page,
            "per_page": params.per_page,
            "search": params.search,
            "sort": params.sort,
        }
        payload = await _request("GET", "/groups", params=query)
        return _ok(payload.get("data"), meta=payload.get("meta"))
    except Exception as e:
        return _error_json(e)


class CreateGroupInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Nome do grupo (único por tenant).", max_length=255, min_length=1)


@mcp.tool(
    name="create_group",
    annotations={
        "title": "Criar Grupo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def create_group(params: CreateGroupInput) -> str:
    """Cria um novo grupo de contatos.

    Args:
        params (CreateGroupInput): name (nome único por tenant).

    Returns:
        str: JSON com {success, data: {id, name, created_at, updated_at}} ou
        erro 422 VALIDATION_ERROR se o nome já existir.
    """
    try:
        payload = await _request("POST", "/groups", json_body={"name": params.name})
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class AddContactsToGroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: int = Field(..., description="ID do grupo.")
    contact_ids: List[int] = Field(
        ..., description="IDs dos contatos a adicionar ao grupo (mínimo 1).", min_length=1
    )


@mcp.tool(
    name="add_contacts_to_group",
    annotations={
        "title": "Adicionar Contatos a um Grupo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def add_contacts_to_group(params: AddContactsToGroupInput) -> str:
    """Adiciona um ou mais contatos a um grupo existente.

    Contatos que já pertencem ao grupo são silenciosamente ignorados
    (operação idempotente). Todos os contact_ids devem pertencer ao mesmo
    tenant e existir previamente.

    Args:
        params (AddContactsToGroupInput): group_id, contact_ids.

    Returns:
        str: JSON com {success, data: {group_id, contacts_added}} ou erro
        404 NOT_FOUND / 422 VALIDATION_ERROR.
    """
    try:
        payload = await _request(
            "POST",
            f"/groups/{params.group_id}/contacts",
            json_body={"contact_ids": params.contact_ids},
        )
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


# ===========================================================================
# TEMPLATES (somente leitura)
# ===========================================================================


class ListTemplatesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    page: int = Field(default=1, ge=1, description="Número da página.")
    per_page: int = Field(default=15, ge=1, le=100, description="Itens por página (máx 100).")
    search: Optional[str] = Field(default=None, description="Busca por nome do template ou categoria.")
    status: Optional[str] = Field(
        default=None, description="Filtrar por status: APPROVED, PENDING ou REJECTED."
    )
    category: Optional[str] = Field(
        default=None, description="Filtrar por categoria: MARKETING, UTILITY ou AUTHENTICATION."
    )
    language: Optional[str] = Field(default=None, description="Filtrar por idioma, ex: 'en', 'pt_BR'.")


@mcp.tool(
    name="list_templates",
    annotations={
        "title": "Listar Templates de WhatsApp",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_templates(params: ListTemplatesInput) -> str:
    """Lista os templates de mensagem sincronizados da conta WhatsApp Business.

    Use antes de send_template_message para confirmar nome exato, idioma,
    categoria, status de aprovação e quantidade de parâmetros esperados.

    Args:
        params (ListTemplatesInput): paginação, busca, filtros por status/
            categoria/idioma.

    Returns:
        str: JSON com {success, data: [{id, template_id, template_name,
        language, category, status, body_data, body_params_count, ...}, ...]}
        e meta de paginação.
    """
    try:
        query = {
            "page": params.page,
            "per_page": params.per_page,
            "search": params.search,
            "filter[status]": params.status,
            "filter[category]": params.category,
            "filter[language]": params.language,
        }
        payload = await _request("GET", "/templates", params=query)
        return _ok(payload.get("data"), meta=payload.get("meta"))
    except Exception as e:
        return _error_json(e)


class GetTemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: int = Field(
        ..., description="ID do template do WhatsApp (campo template_id, não o ID interno)."
    )


@mcp.tool(
    name="get_template",
    annotations={
        "title": "Obter Template",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_template(params: GetTemplateInput) -> str:
    """Retorna os detalhes completos de um template pelo template_id do WhatsApp.

    Args:
        params (GetTemplateInput): template_id.

    Returns:
        str: JSON com {success, data: {...}} ou erro 404 NOT_FOUND.
    """
    try:
        payload = await _request("GET", f"/templates/{params.template_id}")
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


# ===========================================================================
# AUTENTICAÇÃO / OTP
# ===========================================================================


class ListAuthTemplatesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    page: int = Field(default=1, ge=1, description="Número da página.")
    per_page: int = Field(default=15, ge=1, le=100, description="Itens por página.")
    search: Optional[str] = Field(default=None, description="Busca por nome do template.")
    language: Optional[str] = Field(default=None, description="Filtrar por idioma.")


@mcp.tool(
    name="list_auth_templates",
    annotations={
        "title": "Listar Templates de Autenticação (OTP)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_auth_templates(params: ListAuthTemplatesInput) -> str:
    """Lista templates da categoria AUTHENTICATION aprovados, usáveis para OTP.

    Use antes de send_otp para confirmar o template_name correto.

    Args:
        params (ListAuthTemplatesInput): paginação, busca, filtro de idioma.

    Returns:
        str: JSON com {success, data: [{id, template_name, language,
        body_text, body_params_count, ...}, ...]}.
    """
    try:
        query = {
            "page": params.page,
            "per_page": params.per_page,
            "search": params.search,
            "filter[language]": params.language,
        }
        payload = await _request("GET", "/auth/templates", params=query)
        return _ok(payload.get("data"), meta=payload.get("meta"))
    except Exception as e:
        return _error_json(e)


class SendOtpInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    phone: str = Field(..., description="Número de telefone internacional.", pattern=PHONE_PATTERN)
    template_name: str = Field(
        ..., description="Nome de um template AUTHENTICATION aprovado (ver list_auth_templates)."
    )
    code: Optional[str] = Field(
        default=None,
        description="Código de 6 dígitos. Se omitido, é gerado automaticamente pelo servidor.",
        pattern=r"^\d{6}$",
    )
    language: str = Field(default="en", description="Idioma do template.")
    expiry_minutes: int = Field(
        default=10, ge=1, le=60, description="Minutos até o código expirar (1-60)."
    )
    contact_id: Optional[int] = Field(default=None, description="ID interno do contato.")
    purpose: str = Field(
        default="authentication",
        description="Rótulo do propósito do OTP (deve coincidir com o usado em verify/resend/status).",
    )


@mcp.tool(
    name="send_otp",
    annotations={
        "title": "Enviar Código OTP via WhatsApp",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_otp(params: SendOtpInput) -> str:
    """Envia um código OTP de autenticação via template WhatsApp AUTHENTICATION.

    Cria o contato automaticamente se o telefone for novo. O código pode ser
    fornecido manualmente ou gerado automaticamente pelo servidor.

    Args:
        params (SendOtpInput): phone, template_name e campos opcionais
            (code, language, expiry_minutes, contact_id, purpose).

    Returns:
        str: JSON com {success, data: {message_id, contact_id, phone,
        template_name, code_sent, code_auto_generated, code (só se
        auto-gerado), expiry_minutes, expires_at, status, sent_at}} ou erro
        (ex: 404 se template não existir, 429 RATE_LIMIT_EXCEEDED).
    """
    try:
        body = {
            "phone": params.phone,
            "template_name": params.template_name,
            "code": params.code,
            "language": params.language,
            "expiry_minutes": params.expiry_minutes,
            "contact_id": params.contact_id,
            "purpose": params.purpose,
        }
        payload = await _request("POST", "/auth/send-otp", json_body=body)
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class VerifyOtpInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    phone: str = Field(..., description="Número de telefone internacional.", pattern=PHONE_PATTERN)
    code: str = Field(..., description="Código OTP recebido pelo usuário.", pattern=r"^\d{6}$")
    purpose: str = Field(
        default="authentication",
        description="Deve coincidir com o purpose usado em send_otp/resend_otp.",
    )


@mcp.tool(
    name="verify_otp",
    annotations={
        "title": "Verificar Código OTP",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def verify_otp(params: VerifyOtpInput) -> str:
    """Verifica um código OTP enviado anteriormente via send_otp.

    Limite de 10 tentativas por minuto por telefone, e um número máximo de
    tentativas por código (padrão 5, configurável). O contador de
    tentativas é zerado em caso de sucesso.

    Args:
        params (VerifyOtpInput): phone, code, purpose.

    Returns:
        str: JSON com {success, data: {phone, purpose, verified_at}} em
        caso de sucesso, ou erro (OTP_NOT_FOUND, MAX_ATTEMPTS_EXCEEDED,
        OTP_EXPIRED, INVALID_CODE com attempts_remaining).
    """
    try:
        payload = await _request(
            "POST",
            "/auth/verify",
            json_body={"phone": params.phone, "code": params.code, "purpose": params.purpose},
        )
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class ResendOtpInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    phone: str = Field(..., description="Número de telefone internacional.", pattern=PHONE_PATTERN)
    template_name: str = Field(..., description="Nome do template AUTHENTICATION aprovado.")
    language: str = Field(default="en", description="Idioma do template.")
    expiry_minutes: int = Field(default=10, ge=1, le=60, description="Minutos até o novo código expirar.")
    purpose: str = Field(default="authentication", description="Deve coincidir com o purpose original.")


@mcp.tool(
    name="resend_otp",
    annotations={
        "title": "Reenviar Código OTP",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def resend_otp(params: ResendOtpInput) -> str:
    """Gera um novo código OTP (invalidando o anterior) e reenvia via WhatsApp.

    Limite mais restrito que verify: 3 tentativas de reenvio a cada 5
    minutos por telefone.

    Args:
        params (ResendOtpInput): phone, template_name e campos opcionais
            (language, expiry_minutes, purpose).

    Returns:
        str: JSON no mesmo formato de send_otp, ou erro 429
        RATE_LIMIT_EXCEEDED se o limite de reenvio for atingido.
    """
    try:
        body = {
            "phone": params.phone,
            "template_name": params.template_name,
            "language": params.language,
            "expiry_minutes": params.expiry_minutes,
            "purpose": params.purpose,
        }
        payload = await _request("POST", "/auth/resend", json_body=body)
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


class CheckOtpStatusInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    phone: str = Field(..., description="Número de telefone internacional.", pattern=PHONE_PATTERN)
    purpose: str = Field(default="authentication", description="Deve coincidir com o purpose usado ao enviar o OTP.")


@mcp.tool(
    name="check_otp_status",
    annotations={
        "title": "Consultar Status do OTP",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def check_otp_status(params: CheckOtpStatusInput) -> str:
    """Consulta o status de um OTP ativo sem consumir tentativas de verificação.

    Útil para exibir contagem regressiva ou saber quantas tentativas restam
    antes de chamar verify_otp.

    Args:
        params (CheckOtpStatusInput): phone, purpose.

    Returns:
        str: JSON com {success, data: {has_active_otp, expires_at,
        expires_in_seconds, is_expired, verification_attempts, max_attempts,
        attempts_remaining, created_at}} ou {has_active_otp: false, message}
        se não houver OTP ativo.
    """
    try:
        payload = await _request(
            "POST", "/auth/status", json_body={"phone": params.phone, "purpose": params.purpose}
        )
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


# ===========================================================================
# SOURCES E STATUSES (somente leitura — necessário para create_contact)
# ===========================================================================


class ListSourcesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    page: int = Field(default=1, ge=1, description="Número da página.")
    per_page: int = Field(default=15, ge=1, le=100, description="Itens por página (máx 100).")
    search: Optional[str] = Field(default=None, description="Busca por nome da origem.")


@mcp.tool(
    name="list_sources",
    annotations={
        "title": "Listar Origens de Contato (Sources)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_sources(params: ListSourcesInput) -> str:
    """Lista as origens de contato (sources) cadastradas no tenant.

    Necessário para descobrir um source_id válido antes de chamar
    create_contact, já que esse campo é obrigatório e precisa referenciar
    uma origem existente.

    Args:
        params (ListSourcesInput): paginação e busca.

    Returns:
        str: JSON com {success, data: [{id, name, created_at, updated_at}, ...]}
        e meta de paginação.
    """
    try:
        query = {"page": params.page, "per_page": params.per_page, "search": params.search}
        payload = await _request("GET", "/sources", params=query)
        return _ok(payload.get("data"), meta=payload.get("meta"))
    except Exception as e:
        return _error_json(e)


class ListStatusesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    page: int = Field(default=1, ge=1, description="Número da página.")
    per_page: int = Field(default=15, ge=1, le=100, description="Itens por página (máx 100).")
    search: Optional[str] = Field(default=None, description="Busca por nome do status.")


@mcp.tool(
    name="list_statuses",
    annotations={
        "title": "Listar Status de Contato",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_statuses(params: ListStatusesInput) -> str:
    """Lista os status de contato (pipeline) cadastrados no tenant.

    Necessário para descobrir um status_id válido antes de chamar
    create_contact, já que esse campo é obrigatório e precisa referenciar
    um status existente.

    Args:
        params (ListStatusesInput): paginação e busca.

    Returns:
        str: JSON com {success, data: [{id, name, color, created_at,
        updated_at}, ...]} e meta de paginação.
    """
    try:
        query = {"page": params.page, "per_page": params.per_page, "search": params.search}
        payload = await _request("GET", "/statuses", params=query)
        return _ok(payload.get("data"), meta=payload.get("meta"))
    except Exception as e:
        return _error_json(e)


# ===========================================================================
# CONTA
# ===========================================================================


@mcp.tool(
    name="get_account_info",
    annotations={
        "title": "Informações da Conta",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_account_info() -> str:
    """Retorna informações gerais da conta/tenant autenticado.

    Returns:
        str: JSON com {success, data: {id, name, subdomain, status,
        created_at, subscription: {plan_name, plan_id, status,
        expires_at}}}.
    """
    try:
        payload = await _request("GET", "/account")
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


@mcp.tool(
    name="get_usage_stats",
    annotations={
        "title": "Estatísticas de Uso",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_usage_stats() -> str:
    """Retorna o uso atual de cada recurso do plano (contatos, conversas, campanhas, staff).

    Útil para o agente saber, antes de criar contatos/campanhas em massa, se
    ainda há espaço disponível no plano.

    Returns:
        str: JSON com {success, data: {<feature>: {current, limit,
        remaining, percentage_used, is_unlimited}, ...}}.
    """
    try:
        payload = await _request("GET", "/account/usage")
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


@mcp.tool(
    name="get_plan_limits",
    annotations={
        "title": "Limites do Plano",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_plan_limits() -> str:
    """Retorna os limites do plano contratado e quais features estão habilitadas.

    Inclui api_calls_per_minute (rate limit do token atual) — útil para o
    agente espaçar chamadas em lote e evitar 429 RATE_LIMIT_EXCEEDED.

    Returns:
        str: JSON com {success, data: {limits: {contacts, conversations,
        campaigns, staff, api_calls_per_minute}, features: {
        whatsapp_templates, bulk_messaging, chatbot, advanced_analytics,
        api_access, custom_branding}}}.
    """
    try:
        payload = await _request("GET", "/account/limits")
        return _ok(payload.get("data"))
    except Exception as e:
        return _error_json(e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
