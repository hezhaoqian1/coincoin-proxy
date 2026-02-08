from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "coincoin-proxy"
    env: str = "prod"

    # Auth / security
    admin_token: str = "change-me"
    webhook_secret: str = "change-me-webhook"  # 支付回调验证
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
    
    # Pricing (cents per million tokens)
    # Default: Input $1.75/M = 175 cents/M, Output $14/M = 1400 cents/M
    price_input_per_million: int = 175  # 单位：分/百万 tokens
    price_output_per_million: int = 1400  # 单位：分/百万 tokens
    
    # Billing mode: "balance" (扣余额) or "token_limit" (扣 token 额度) or "none" (不限制)
    billing_mode: str = "balance"

    class Config:
        env_prefix = "COINCOIN_"
        env_file = ".env"
        extra = "ignore"


settings = Settings()
