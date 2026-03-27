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
    embedding_model: str = "text-embedding-3-small"
    model_catalog_path: str = "config/model_catalog.json"
    model_catalog_json: str = ""

    # Gateway-backed public models (LiteLLM / internal OpenAI-compatible gateway)
    gateway_base_url: str = ""
    gateway_api_key: str = ""
    gateway_auth_style: str = "bearer"

    # Optional direct Vertex lane for explicit ops/debug fallback.
    # Long-term production Gemini traffic should prefer the internal gateway.
    vertex_api_key: str = ""
    vertex_gemini_api_base: str = "https://aiplatform.googleapis.com/v1/publishers/google"

    # Cached tokens discount (fraction of input price charged for cached tokens)
    # Azure Standard: typically 50% off => 0.5, Provisioned can be 100% off => 0.0
    cache_discount_rate: float = 0.5

    # Async multi-image jobs
    image_jobs_enabled: bool = True
    image_job_poll_interval: int = 5
    image_job_storage_dir: str = "/tmp/coincoin-image-jobs"
    image_job_sync_input_limit: int = 2
    image_job_async_max_inputs: int = 8
    image_job_max_total_bytes: int = 50 * 1024 * 1024

    # Database — prefer database_url (Railway ${{MySQL.MYSQL_URL}});
    # falls back to individual db_host/port/name/user/password fields.
    database_url: str = ""
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
    # Input $0.99/M = 99 cents/M, Output $6.99/M = 699 cents/M
    price_input_per_million: int = 99  # 单位：分/百万 tokens
    price_output_per_million: int = 699  # 单位：分/百万 tokens
    
    # Billing mode: "balance" (扣余额) or "token_limit" (扣 token 额度) or "none" (不限制)
    billing_mode: str = "balance"
    
    # 新用户默认余额（分），0 = 需要充值才能使用
    default_balance: int = 0

    # Referral system
    referral_commission_rate: float = 0.05  # 5%
    referral_max_rewards_per_user: int = 3  # only first 3 orders from each referred user
    referral_reward_cap_cents: int = 5000  # max $50 cumulative per referred user
    referral_new_user_bonus_cents: int = 300  # $3 bonus for referred user on first purchase

    # Payment (direct Epay integration)
    epay_api_url: str = ""
    epay_pid: str = ""
    epay_key: str = ""
    epay_site_name: str = "CoinCoin"
    # Legacy bird-alipay query bridge. Kept only as an optional fallback path.
    pay_base_url: str = ""
    rmb_to_cents_rate: float = 14.0  # 1 RMB ≈ 14 cents ($0.14)
    self_base_url: str = ""  # public URL of this proxy, used for Epay notify/return callback

    # Router
    router_enabled: bool = False
    router_tool_count_threshold: int = 2

    # Force strip unsupported params (temperature, top_p, etc.) on primary model
    # even when model name doesn't contain 'codex'. Needed for upstream proxies
    # backed by ChatGPT that reject these params.
    primary_strip_unsupported: bool = False

    # Auth style: "azure" uses api-key header, "bearer" uses Authorization: Bearer
    primary_auth_style: str = "azure"

    # Model identity cloak: when True, inject an instruction telling the model
    # to always identify itself as the display_model (what the user requested),
    # hiding the real upstream model name from conversational probing.
    model_cloak: bool = True

    # Cheap model (optional)
    cheap_model: str = ""
    cheap_upstream_url: str = ""  # empty = use main upstream
    cheap_api_key: str = ""  # empty = use main api key
    cheap_price_input: int = 15  # cents per million tokens
    cheap_price_output: int = 60  # cents per million tokens

    # Fallback model (reliable backend used when primary or cheap upstream fails)
    fallback_model: str = ""
    fallback_upstream_url: str = ""  # empty = use main upstream
    fallback_api_key: str = ""  # empty = use main api key
    fallback_price_input: int = 99  # cents per million tokens
    fallback_price_output: int = 699  # cents per million tokens
    fallback_auth_style: str = ""  # empty = inherit primary_auth_style

    class Config:
        env_prefix = "COINCOIN_"
        env_file = ".env"
        extra = "ignore"


settings = Settings()
