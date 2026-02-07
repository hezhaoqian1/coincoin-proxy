from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "coincoin-proxy"
    env: str = "prod"

    # Auth / security
    admin_token: str = "change-me"
    key_prefix: str = "sk_cc_"
    key_pepper: str = "coincoin-pepper"

    # Upstream (Azure OpenAI compatible)
    upstream_base_url: str = "https://hc-instance-eastus2.cognitiveservices.azure.com/openai/v1"
    upstream_api_key: str = ""
    fixed_model: str = "gpt-5.2-codex"

    # Database
    db_host: str = ""
    db_port: int = 4000
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""
    db_pool_size: int = 10

    # Usage flush (seconds)
    usage_flush_interval: int = 5

    # Performance
    http_pool_max: int = 100
    http_pool_keepalive: int = 20
    key_cache_ttl: int = 30
    key_cache_max: int = 10000

    class Config:
        env_prefix = "COINCOIN_"
        env_file = ".env"
        extra = "ignore"


settings = Settings()
