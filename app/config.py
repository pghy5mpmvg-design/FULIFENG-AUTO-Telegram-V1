import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


@dataclass
class Settings:
    bot_token: str = field(default_factory=lambda: os.getenv('BOT_TOKEN', '').strip())
    openai_api_key: str = field(default_factory=lambda: os.getenv('OPENAI_API_KEY', '').strip())
    openai_model: str = field(default_factory=lambda: os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'))
    database_url: str = field(default_factory=lambda: os.getenv('DATABASE_URL', 'sqlite:///./data/bot.db'))
    timezone: str = field(default_factory=lambda: os.getenv('TZ', 'Europe/Moscow'))
    admin_ids: set[int] = field(default_factory=lambda: {int(x.strip()) for x in os.getenv('ADMIN_USER_IDS', '').split(',') if x.strip()})
    target_chat: str = field(default_factory=lambda: os.getenv('TARGET_CHAT_ID', '').strip())
    stock_text: str = field(default_factory=lambda: os.getenv('STOCK_TEXT', 'Наличие автомобилей уточняется. Напишите интересующую модель.'))
    price_text: str = field(default_factory=lambda: os.getenv('PRICE_TEXT', 'Стоимость рассчитывается индивидуально. Укажите модель, год и город доставки.'))

    def __post_init__(self):
        ZoneInfo(self.timezone)
        if self.database_url.startswith('postgres://'):
            self.database_url = self.database_url.replace('postgres://', 'postgresql+psycopg://', 1)
        elif self.database_url.startswith('postgresql://'):
            self.database_url = self.database_url.replace('postgresql://', 'postgresql+psycopg://', 1)

