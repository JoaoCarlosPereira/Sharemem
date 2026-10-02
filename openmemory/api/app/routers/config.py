import copy
import json
import logging
from typing import Any, Dict, Optional

from app.database import get_db
from app.models import Config as ConfigModel
from app.models import get_current_utc_time
from app.utils.admin_auth import require_admin
from app.utils.memory import reset_memory_client
from app.utils.secret_mask import (
    MaskedSecretError,
    assert_no_masked_secrets,
    mask_config_secrets,
    restore_masked_secrets,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Toda a superfície de config exige admin (ADMIN_TOKEN ou sessão JWT, ver
# ``require_admin``): a config contém a ``api_key`` do LLM e o ``openai_base_url``
# — leitura vaza segredo, escrita permite repontar o LLM para um servidor
# arbitrário. A dependência fica no APIRouter (não por rota) para ser
# fail-closed: qualquer rota nova adicionada aqui herda a proteção.


class SecretMaskingRoute(APIRoute):
    """Rota que mascara segredos em TODA resposta JSON do router.

    Centraliza o mascaramento (não depende de cada handler lembrar de chamar
    ``mask_config_secrets``): rotas novas neste router herdam automaticamente.
    Erros levantados como ``HTTPException`` são serializados fora deste wrapper
    (exception handlers do app) — não ponha segredos em ``detail``.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            response = await original(request)
            media_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            if media_type != "application/json" or not getattr(response, "body", None):
                return response
            try:
                payload = json.loads(response.body)
            except ValueError:
                return response
            masked = mask_config_secrets(payload)
            if masked == payload:
                return response
            headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
            return JSONResponse(
                content=masked,
                status_code=response.status_code,
                headers=headers,
                background=response.background,
            )

        return handler


router = APIRouter(
    prefix="/api/v1/config",
    tags=["config"],
    dependencies=[Depends(require_admin)],
    route_class=SecretMaskingRoute,
)

class LLMConfig(BaseModel):
    model: str = Field(..., description="LLM model name")
    temperature: float = Field(..., description="Temperature setting for the model")
    max_tokens: int = Field(..., description="Maximum tokens to generate")
    api_key: Optional[str] = Field(None, description="API key or 'env:API_KEY' to use environment variable")
    ollama_base_url: Optional[str] = Field(None, description="Base URL for Ollama server (e.g., http://host.docker.internal:11434)")
    openai_base_url: Optional[str] = Field(
        None,
        description="OpenAI-compatible API base URL for local llama.cpp/LM Studio (e.g., http://host.docker.internal:8000/v1)",
    )

class LLMProvider(BaseModel):
    provider: str = Field(..., description="LLM provider name")
    config: LLMConfig

class EmbedderConfig(BaseModel):
    model: str = Field(..., description="Embedder model name")
    api_key: Optional[str] = Field(None, description="API key or 'env:API_KEY' to use environment variable")
    ollama_base_url: Optional[str] = Field(None, description="Base URL for Ollama server (e.g., http://host.docker.internal:11434)")
    openai_base_url: Optional[str] = Field(
        None,
        description="OpenAI-compatible embedding API base URL (local llama.cpp/LM Studio)",
    )

class EmbedderProvider(BaseModel):
    provider: str = Field(..., description="Embedder provider name")
    config: EmbedderConfig

class VectorStoreProvider(BaseModel):
    provider: str = Field(..., description="Vector store provider name")
    # Below config can vary widely based on the vector store used. Refer https://docs.mem0.ai/components/vectordbs/config
    config: Dict[str, Any] = Field(..., description="Vector store-specific configuration")

class OpenMemoryConfig(BaseModel):
    custom_instructions: Optional[str] = Field(None, description="Custom instructions for memory management and fact extraction")
    multilingual: Optional[bool] = Field(
        True,
        description="When True, extract memories in the same language as the input messages",
    )

class Mem0Config(BaseModel):
    llm: Optional[LLMProvider] = None
    embedder: Optional[EmbedderProvider] = None
    vector_store: Optional[VectorStoreProvider] = None

class ConfigSchema(BaseModel):
    openmemory: Optional[OpenMemoryConfig] = None
    mem0: Optional[Mem0Config] = None


def _model_dump(model: BaseModel, **kwargs) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    return model.dict(**kwargs)


def get_default_configuration():
    """Get the default configuration with sensible defaults for LLM and embedder.

    The LLM/embedder blocks are derived from the environment
    (``LLM_PROVIDER``/``EMBEDDER_PROVIDER`` — ``ollama`` in the local-first
    deployment) instead of being hardcoded to OpenAI, so seeding this default
    into the DB never silently reintroduces cloud egress. ``vector_store`` stays
    ``None`` so ``get_memory_client`` keeps auto-detecting it from the env.
    """
    from app.utils.memory import get_default_memory_config

    base = get_default_memory_config()
    return {
        "openmemory": {
            "custom_instructions": None,
            "multilingual": True,
        },
        "mem0": {
            "llm": base.get("llm"),
            "embedder": base.get("embedder"),
            "vector_store": None
        }
    }

def get_config_from_db(db: Session, key: str = "main"):
    """Get configuration from database."""
    config = db.query(ConfigModel).filter(ConfigModel.key == key).first()
    
    if not config:
        # Create default config with proper provider configurations
        default_config = get_default_configuration()
        db_config = ConfigModel(key=key, value=default_config)
        db.add(db_config)
        db.commit()
        db.refresh(db_config)
        return default_config
    
    # Ensure the config has all required sections with defaults
    config_value = config.value
    default_config = get_default_configuration()
    
    # Merge with defaults to ensure all required fields exist
    if "openmemory" not in config_value:
        config_value["openmemory"] = default_config["openmemory"]
    elif config_value["openmemory"].get("multilingual") is None:
        config_value["openmemory"]["multilingual"] = default_config["openmemory"]["multilingual"]
    
    if "mem0" not in config_value:
        config_value["mem0"] = default_config["mem0"]
    else:
        # Ensure LLM config exists with defaults
        if "llm" not in config_value["mem0"] or config_value["mem0"]["llm"] is None:
            config_value["mem0"]["llm"] = default_config["mem0"]["llm"]
        
        # Ensure embedder config exists with defaults
        if "embedder" not in config_value["mem0"] or config_value["mem0"]["embedder"] is None:
            config_value["mem0"]["embedder"] = default_config["mem0"]["embedder"]
        
        # Ensure vector_store config exists with defaults
        if "vector_store" not in config_value["mem0"]:
            config_value["mem0"]["vector_store"] = default_config["mem0"]["vector_store"]

    # Save the updated config back to database if it was modified
    if config_value != config.value:
        config.value = config_value
        db.commit()
        db.refresh(config)
    
    return config_value

def save_config_to_db(db: Session, config: Dict[str, Any], key: str = "main"):
    """Save configuration to database.

    Garantia absoluta: se sobrou máscara (``****…``) em qualquer posição que
    ``mask_config_secrets`` mascararia (chave sensível, URL com credencial), a
    escrita é rejeitada com 422 — a máscara devolvida pelo GET nunca vira o
    segredo persistido. Texto livre (ex.: ``custom_instructions``) começando
    com ``****`` não bloqueia (ver ``find_masked_values``).
    """
    try:
        assert_no_masked_secrets(config)
    except MaskedSecretError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    db_config = db.query(ConfigModel).filter(ConfigModel.key == key).first()

    if db_config:
        db_config.value = config
        db_config.updated_at = get_current_utc_time()
    else:
        db_config = ConfigModel(key=key, value=config)
        db.add(db_config)

    db.commit()
    db.refresh(db_config)
    return db_config.value


def _replace_mem0_section(db: Session, section: str, payload: BaseModel) -> Dict[str, Any]:
    """Substitui ``mem0.<section>`` (llm/embedder/vector_store) e persiste.

    Segredo mascarado vindo do GET mantém o valor real já persistido.
    """
    current_config = copy.deepcopy(get_config_from_db(db))
    if not isinstance(current_config.get("mem0"), dict):
        current_config["mem0"] = {}
    new_section = _model_dump(payload, exclude_none=True)
    restore_masked_secrets(new_section, current_config["mem0"].get(section))
    current_config["mem0"][section] = new_section
    save_config_to_db(db, current_config)
    reset_memory_client()
    return new_section


# Respostas: o mascaramento é feito por ``SecretMaskingRoute`` (todas as rotas).


@router.get("", response_model=ConfigSchema)
@router.get("/", response_model=ConfigSchema)
async def get_configuration(db: Session = Depends(get_db)):
    """Get the current configuration (secrets masked)."""
    return get_config_from_db(db)


@router.put("", response_model=ConfigSchema)
@router.put("/", response_model=ConfigSchema)
async def update_configuration(config: ConfigSchema, db: Session = Depends(get_db)):
    """Update the configuration."""
    try:
        current_config = get_config_from_db(db)
        # Cópia profunda: além de preservar o snapshot para restaurar segredos
        # mascarados, garante que o SQLAlchemy veja um JSON novo ao salvar.
        updated_config = copy.deepcopy(current_config)

        if config.openmemory is not None:
            if not isinstance(updated_config.get("openmemory"), dict):
                updated_config["openmemory"] = {}
            updated_config["openmemory"].update(_model_dump(config.openmemory, exclude_none=True))

        if config.mem0 is not None:
            mem0_update = _model_dump(config.mem0, exclude_none=True)
            # GET devolve segredos mascarados: a máscara nunca sobrescreve o real.
            restore_masked_secrets(mem0_update, current_config.get("mem0"))
            if not isinstance(updated_config.get("mem0"), dict):
                updated_config["mem0"] = {}
            # Merge section-by-section so UI saves (llm/embedder only) keep vector_store.
            for key, value in mem0_update.items():
                updated_config["mem0"][key] = value

        save_config_to_db(db, updated_config)
        reset_memory_client()
        return updated_config
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to update configuration")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save configuration: {exc}",
        ) from exc


@router.patch("", response_model=ConfigSchema)
@router.patch("/", response_model=ConfigSchema)
async def patch_configuration(config_update: ConfigSchema, db: Session = Depends(get_db)):
    """Update parts of the configuration."""
    current_config = copy.deepcopy(get_config_from_db(db))

    def deep_update(source, overrides):
        for key, value in overrides.items():
            if isinstance(value, dict) and key in source and isinstance(source[key], dict):
                source[key] = deep_update(source[key], value)
            else:
                source[key] = value
        return source

    update_data = _model_dump(config_update, exclude_unset=True)
    # Antes do merge: máscara vinda do GET mantém o segredo persistido.
    restore_masked_secrets(update_data, current_config)
    updated_config = deep_update(current_config, update_data)

    save_config_to_db(db, updated_config)
    reset_memory_client()
    return updated_config


@router.post("/reset", response_model=ConfigSchema)
async def reset_configuration(db: Session = Depends(get_db)):
    """Reset the configuration to default values."""
    try:
        default_config = get_default_configuration()
        save_config_to_db(db, default_config)
        reset_memory_client()
        return default_config
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset configuration: {str(e)}"
        )


@router.get("/mem0/llm", response_model=LLMProvider)
async def get_llm_configuration(db: Session = Depends(get_db)):
    """Get only the LLM configuration."""
    return get_config_from_db(db).get("mem0", {}).get("llm", {})


@router.put("/mem0/llm", response_model=LLMProvider)
async def update_llm_configuration(llm_config: LLMProvider, db: Session = Depends(get_db)):
    """Update only the LLM configuration."""
    return _replace_mem0_section(db, "llm", llm_config)


@router.get("/mem0/embedder", response_model=EmbedderProvider)
async def get_embedder_configuration(db: Session = Depends(get_db)):
    """Get only the Embedder configuration."""
    return get_config_from_db(db).get("mem0", {}).get("embedder", {})


@router.put("/mem0/embedder", response_model=EmbedderProvider)
async def update_embedder_configuration(embedder_config: EmbedderProvider, db: Session = Depends(get_db)):
    """Update only the Embedder configuration."""
    return _replace_mem0_section(db, "embedder", embedder_config)


@router.get("/mem0/vector_store", response_model=Optional[VectorStoreProvider])
async def get_vector_store_configuration(db: Session = Depends(get_db)):
    """Get only the Vector Store configuration."""
    return get_config_from_db(db).get("mem0", {}).get("vector_store", None)


@router.put("/mem0/vector_store", response_model=VectorStoreProvider)
async def update_vector_store_configuration(vector_store_config: VectorStoreProvider, db: Session = Depends(get_db)):
    """Update only the Vector Store configuration."""
    return _replace_mem0_section(db, "vector_store", vector_store_config)


@router.get("/openmemory", response_model=OpenMemoryConfig)
async def get_openmemory_configuration(db: Session = Depends(get_db)):
    """Get only the OpenMemory configuration."""
    return get_config_from_db(db).get("openmemory", {})


@router.put("/openmemory", response_model=OpenMemoryConfig)
async def update_openmemory_configuration(openmemory_config: OpenMemoryConfig, db: Session = Depends(get_db)):
    """Update only the OpenMemory configuration."""
    current_config = copy.deepcopy(get_config_from_db(db))
    if not isinstance(current_config.get("openmemory"), dict):
        current_config["openmemory"] = {}
    current_config["openmemory"].update(_model_dump(openmemory_config, exclude_none=True))
    save_config_to_db(db, current_config)
    reset_memory_client()
    return current_config["openmemory"]
