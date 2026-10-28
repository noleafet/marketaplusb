"""Django settings for the grid suitability engine."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent

env = environ.Env(
    DEBUG=(bool, False),
    TIME_ZONE=(str, "UTC"),
    TIMESCALE_ENABLED=(bool, False),
    ENGINE_ALLOW_PUBLIC_READS=(bool, True),
    LIVE_TRADING_ENABLED=(bool, False),
    BINANCE_FUTURES_TESTNET=(bool, True),
)
environ.Env.read_env(ROOT_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
TIMESCALE_ENABLED = env("TIMESCALE_ENABLED")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "apps.coins",
    "apps.engine",
    "apps.trading",
    "apps.jobs",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=env(
            "POSTGRES_URI",
            default="postgresql://grid:gridpass@localhost:5432/grid",
        ),
    )
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"]["connect_timeout"] = 10

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS", default=["http://localhost:3000", "http://127.0.0.1:3000"]
)
CORS_ALLOW_CREDENTIALS = False

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_PERMISSION_CLASSES": ["apps.common.permissions.IsEngineTokenOrReadOnly"],
    "UNAUTHENTICATED_USER": None,
}

# ---------------------------------------------------------------- security
ENGINE_API_TOKEN = env("ENGINE_API_TOKEN", default="")
ENGINE_ALLOW_PUBLIC_READS = env("ENGINE_ALLOW_PUBLIC_READS")

# ------------------------------------------------------------ market data
COINGECKO_API_KEY = env("COINGECKO_API_KEY", default="")
COINGECKO_BASE_URL = env("COINGECKO_BASE_URL", default="https://api.coingecko.com/api/v3")
BINANCE_FUTURES_BASE_URL = env(
    "BINANCE_FUTURES_BASE_URL", default="https://fapi.binance.com"
)
BINANCE_SPOT_BASE_URL = env("BINANCE_SPOT_BASE_URL", default="https://api.binance.com")

# ----------------------------------------------------------------- engine
ENGINE = {
    "LOOKBACK_DAYS": env.int("ENGINE_LOOKBACK_DAYS", default=520),
    "INTERVAL": env("ENGINE_INTERVAL", default="4h"),
    "SYMBOL_LIMIT": env.int("ENGINE_SYMBOL_LIMIT", default=0),
    "TOP_N_PAPER": env.int("ENGINE_TOP_N_PAPER", default=8),
    "PAPER_CAPITAL": env.float("ENGINE_PAPER_CAPITAL", default=1000.0),
    "LEVERAGE": env.float("ENGINE_LEVERAGE", default=2.0),
    "MAKER_FEE_RATE": env.float("ENGINE_MAKER_FEE_RATE", default=0.0002),
    "TAKER_FEE_RATE": env.float("ENGINE_TAKER_FEE_RATE", default=0.0005),
    "SLIPPAGE_BPS": env.float("ENGINE_SLIPPAGE_BPS", default=1.0),
    "INITIAL_INVENTORY_PCT": env.float("ENGINE_INITIAL_INVENTORY_PCT", default=0.4),
    "MIN_ADV_USD": env.float("ENGINE_MIN_ADV_USD", default=5_000_000.0),
    "MIN_OOS_TRADES": env.int("ENGINE_MIN_OOS_TRADES", default=12),
    "MAX_DRAWDOWN": env.float("ENGINE_MAX_DRAWDOWN", default=0.35),
    "RANGE_LOOKBACK_DAYS": env.int("ENGINE_RANGE_LOOKBACK_DAYS", default=14),
    "GRID_LEVELS": env.int("ENGINE_GRID_LEVELS", default=20),
    "ATR_MULTIPLIER": env.float("ENGINE_ATR_MULTIPLIER", default=1.0),
    "REBALANCE_DAYS": env.int("ENGINE_REBALANCE_DAYS", default=7),
    "STOP_LOSS_PCT": env.float("ENGINE_STOP_LOSS_PCT", default=0.30),
    "WARMUP_BUFFER_DAYS": env.int("ENGINE_WARMUP_BUFFER_DAYS", default=45),
    "CANDLE_SYNC_TIMEOUT": env.int("ENGINE_CANDLE_SYNC_TIMEOUT", default=30),
}

PAPER = {
    "TICK_MINUTES": env.int("PAPER_TICK_MINUTES", default=5),
    "MIN_DAYS": env.float("PAPER_EVAL_MIN_DAYS", default=7),
    "MIN_NET_RETURN": env.float("PAPER_MIN_NET_RETURN", default=0.01),
    "MAX_DRAWDOWN": env.float("PAPER_MAX_DRAWDOWN", default=0.12),
    "MIN_TRADES": env.int("PAPER_MIN_TRADES", default=20),
    "HARD_STOP_DRAWDOWN": env.float("PAPER_HARD_STOP_DRAWDOWN", default=0.20),
    "EVAL_INTERVAL_HOURS": env.int("PAPER_EVAL_INTERVAL_HOURS", default=6),
}

# ------------------------------------ overbought pause / resume risk guard
RISK = {
    "ENABLED": env.bool("RISK_GUARD_ENABLED", default=True),
    "INTERVAL_SECONDS": env.int("RISK_INTERVAL_SECONDS", default=300),
    "RSI_PERIOD": env.int("RISK_RSI_PERIOD", default=14),
    "RSI_OVERBOUGHT": env.float("RISK_RSI_OVERBOUGHT", default=75.0),
    "RSI_RECOVERY": env.float("RISK_RSI_RECOVERY", default=35.0),
    "TRAILING_STOP_PCT": env.float("RISK_TRAILING_STOP_PCT", default=0.06),
    "MACD_FAST": env.int("RISK_MACD_FAST", default=12),
    "MACD_SLOW": env.int("RISK_MACD_SLOW", default=26),
    "MACD_SIGNAL": env.int("RISK_MACD_SIGNAL", default=9),
    "MACD_SHRINK_BARS": env.int("RISK_MACD_SHRINK_BARS", default=2),
    "OI_PERIOD": env("RISK_OI_PERIOD", default="1h"),
    "OI_LOOKBACK": env.int("RISK_OI_LOOKBACK", default=6),
    "OI_DROP_PCT": env.float("RISK_OI_DROP_PCT", default=0.05),
    "OI_NEAR_HIGH_PCT": env.float("RISK_OI_NEAR_HIGH_PCT", default=0.03),
    "OI_FAILSAFE_MINUTES": env.int("RISK_OI_FAILSAFE_MINUTES", default=60),
    "ARM_TIMEOUT_HOURS": env.int("RISK_ARM_TIMEOUT_HOURS", default=24),
    "RECOVERY_COOLDOWN_MINUTES": env.int("RISK_RECOVERY_COOLDOWN_MINUTES", default=30),
}

# ---------------------------------------------------------- live trading
LIVE = {
    "ENABLED": env("LIVE_TRADING_ENABLED"),
    "TESTNET": env("BINANCE_FUTURES_TESTNET"),
    "API_KEY": env("BINANCE_API_KEY", default=""),
    "API_SECRET": env("BINANCE_API_SECRET", default=""),
    "MAX_NOTIONAL_USD": env.float("LIVE_MAX_NOTIONAL_USD", default=250.0),
    "MAX_DAILY_LOSS_USD": env.float("LIVE_MAX_DAILY_LOSS_USD", default=50.0),
    "MAX_LEVERAGE": env.float("LIVE_MAX_LEVERAGE", default=2.0),
    "SYMBOL_ALLOWLIST": [
        s.strip().upper()
        for s in env("LIVE_SYMBOL_ALLOWLIST", default="").split(",")
        if s.strip()
    ],
}

APPEND_SLASH = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "engine": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "market": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "jobs": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
