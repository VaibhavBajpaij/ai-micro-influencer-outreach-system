"""Application configuration and niche campaign presets."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class NichePreset:
    """Configuration preset for discovering and classifying creators in a niche."""

    key: str
    display_name: str
    youtube_queries: tuple[str, ...]
    bluesky_queries: tuple[str, ...]
    positive_keywords: tuple[str, ...]
    theme_keywords: dict[str, tuple[str, ...]]
    brand_safety_excluded: tuple[str, ...]
    default_audience_segment: str


NICHE_PRESETS: dict[str, NichePreset] = {
    "technology": NichePreset(
        key="technology",
        display_name="Technology & AI Gadgets",
        youtube_queries=(
            "n8n ai agent tutorial",
            "cursor ai coding workflow",
            "local llm ollama guide",
            "mini pc homelab setup",
            "budget mechanical keyboard unboxing",
            "home assistant smart home dashboard",
            "obsidian ai second brain workflow",
            "ergonomic desk setup tour",
            "budget anc earbuds review",
            "linux laptop developer setup",
            "portable ssd usb c hub test",
            "claude code mcp workflow",
            "indie dev desk accessories",
            "e ink tablet productivity review",
        ),
        bluesky_queries=(
            "software engineer ai tools",
            "indie hacker saas builder",
            "linux open source developer",
            "tech newsletter gadgets",
            "developer advocate",
        ),
        positive_keywords=(
            "tech",
            "gadget",
            "gadgets",
            "review",
            "reviews",
            "ai",
            "artificial intelligence",
            "llm",
            "software",
            "app",
            "apps",
            "productivity",
            "keyboard",
            "laptop",
            "smartphone",
            "setup",
            "desk",
            "coding",
            "developer",
            "programming",
            "homelab",
            "smart home",
            "pc",
            "hardware",
            "apple",
            "android",
            "linux",
            "automation",
            "workflow",
            "unboxing",
            "earbuds",
            "monitor",
            "ssd",
            "camera",
        ),
        theme_keywords={
            "AI & Automation Tools": ("ai", "llm", "gpt", "claude", "gemini", "ollama", "agent", "automation", "copilot"),
            "Desk Setup & Peripherals": ("desk", "setup", "keyboard", "mouse", "monitor", "ergonomic", "workspace"),
            "Consumer Gadgets & Hardware": ("gadget", "review", "unboxing", "smartphone", "earbuds", "tablet", "wearable"),
            "Laptops, Mini PCs & Homelab": ("laptop", "mini pc", "homelab", "server", "nas", "linux", "ssd", "gpu"),
            "Developer & Productivity Workflows": ("coding", "developer", "programming", "notion", "obsidian", "workflow", "productivity"),
            "Smart Home & IoT": ("smart home", "home assistant", "iot", "matter", "zigbee", "lighting"),
        },
        brand_safety_excluded=(
            "online casino",
            "gambling",
            "sports betting",
            "onlyfans",
            "porn",
            "crack download",
            "pirated",
            "keygen",
            "pump and dump",
            "forex signal scam",
        ),
        default_audience_segment="Tech enthusiasts, developers, and early-adopter gear buyers (18-40)",
    ),
    "fashion_beauty": NichePreset(
        key="fashion_beauty",
        display_name="Fashion & Beauty",
        youtube_queries=(
            "skincare routine honest review",
            "minimalist capsule wardrobe haul",
            "everyday makeup tutorial affordable",
            "clean beauty product review",
            "streetwear outfit styling lookbook",
            "korean skincare glass skin routine",
        ),
        bluesky_queries=(
            "skincare beauty creator",
            "fashion stylist sustainable",
        ),
        positive_keywords=(
            "beauty",
            "skincare",
            "makeup",
            "fashion",
            "style",
            "outfit",
            "wardrobe",
            "cosmetics",
            "serum",
            "cleanser",
            "spf",
            "lookbook",
            "haul",
            "aesthetic",
            "clothing",
            "sustainable",
        ),
        theme_keywords={
            "Skincare & Routines": ("skincare", "serum", "cleanser", "spf", "routine", "dermatology"),
            "Makeup & Beauty Reviews": ("makeup", "foundation", "lip", "mascara", "palette", "beauty"),
            "Styling & Wardrobe": ("outfit", "wardrobe", "capsule", "streetwear", "fashion", "lookbook"),
            "Clean & Sustainable Fashion": ("sustainable", "thrift", "vintage", "clean beauty"),
        },
        brand_safety_excluded=("casino", "gambling", "18+", "onlyfans", "porn"),
        default_audience_segment="Style-conscious beauty & fashion shoppers (18-34)",
    ),
    "fitness": NichePreset(
        key="fitness",
        display_name="Fitness & Wellness",
        youtube_queries=(
            "home workout routine kettlebell",
            "hypertrophy training science based",
            "running gear shoes review",
            "high protein meal prep recipes",
            "mobility stretching routine desk",
        ),
        bluesky_queries=(
            "fitness coach nutrition",
            "running marathon training",
        ),
        positive_keywords=(
            "fitness",
            "workout",
            "gym",
            "training",
            "strength",
            "cardio",
            "running",
            "protein",
            "nutrition",
            "mobility",
            "wellness",
            "calisthenics",
            "hypertrophy",
            "marathon",
            "health",
        ),
        theme_keywords={
            "Strength & Hypertrophy": ("gym", "strength", "hypertrophy", "lifting", "kettlebell", "calisthenics"),
            "Running & Endurance": ("running", "marathon", "cardio", "trail", "shoes"),
            "Nutrition & Meal Prep": ("protein", "meal prep", "nutrition", "macros", "diet"),
            "Mobility & Recovery": ("mobility", "stretching", "recovery", "yoga", "posture"),
        },
        brand_safety_excluded=("steroids", "sarms", "casino", "gambling", "onlyfans"),
        default_audience_segment="Health-focused gym-goers, runners, and wellness enthusiasts (18-44)",
    ),
    "fintech": NichePreset(
        key="fintech",
        display_name="Fintech & Personal Finance",
        youtube_queries=(
            "budgeting app review personal finance",
            "index fund investing beginners",
            "credit card rewards travel points",
            "fintech tools freelancers taxes",
            "side hustle cash flow tracking",
        ),
        bluesky_queries=(
            "fintech personal finance",
            "investing budgeting creator",
        ),
        positive_keywords=(
            "finance",
            "fintech",
            "investing",
            "budget",
            "budgeting",
            "credit",
            "savings",
            "etf",
            "index",
            "wealth",
            "money",
            "portfolio",
            "taxes",
            "banking",
            "dividend",
        ),
        theme_keywords={
            "Personal Budgeting & Apps": ("budget", "budgeting", "savings", "expense", "app"),
            "Long-Term Investing & ETFs": ("investing", "etf", "index", "dividend", "portfolio", "retirement"),
            "Fintech & Banking Tools": ("fintech", "banking", "neobank", "credit card", "rewards"),
        },
        brand_safety_excluded=("guaranteed return", "get rich quick", "casino", "gambling", "ponzi"),
        default_audience_segment="Young professionals and retail investors seeking smarter money tools (22-45)",
    ),
}


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Campaign & Brand
    brand_name: str = "VoltFlow"
    brand_product: str = "VoltFlow Workspace — an AI-powered desk hardware & developer productivity suite"
    brand_value_prop: str = (
        "early access to our flagship smart-dock + paid creator sponsorship and 20% recurring affiliate rev-share"
    )
    default_niche: str = "technology"

    # LLM Providers
    llm_provider: str = "auto"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-haiku-latest"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"

    # Discovery & Filtering
    target_discovery_count: int = 85
    min_followers: int = 5000
    max_followers: int = 100000
    min_engagement_rate_pct: float = 0.4
    min_high_engagement_nano_followers: int = 2000
    high_engagement_nano_rate_pct: float = 2.0
    max_days_since_last_post: int = 120
    require_verified_recency: bool = True
    min_avg_views_for_youtube: int = 500
    min_niche_relevance_score: float = 0.33

    # API Security
    api_auth_token: str = Field(
        default="",
        validation_alias=AliasChoices("API_AUTH_TOKEN", "OUTREACH_API_KEY"),
    )
    allow_live_smtp: bool = False

    # Sending
    send_mode: Literal["simulate", "smtp"] = "simulate"
    stale_sending_timeout_seconds: int = 300
    sender_name: str = "Alex Rivera"
    sender_email: str = "partnerships@voltflow.example"
    reply_to_email: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_use_ssl: bool = False
    smtp_user: str = ""
    smtp_password: str = ""

    # Storage
    database_path: str = "data/outreach.db"
    outputs_dir: str = "outputs"

    def resolve_database_path(self) -> Path:
        path = Path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_outputs_dir(self) -> Path:
        path = Path(self.outputs_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path


def get_niche_preset(niche_key: str) -> NichePreset:
    """Return the configured preset or raise a clear ValueError for unknown niches."""
    normalized = niche_key.strip().lower()
    if normalized not in NICHE_PRESETS:
        supported = ", ".join(sorted(NICHE_PRESETS.keys()))
        raise ValueError(f"Unsupported niche '{niche_key}'. Supported presets: {supported}")
    return NICHE_PRESETS[normalized]
