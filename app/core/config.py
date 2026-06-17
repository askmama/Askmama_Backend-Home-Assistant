from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MQTT_BROKER: str
    MQTT_PORT: int = 8883
    MQTT_USERNAME: str
    MQTT_PASSWORD: str
    SUPABASE_URL: str
    SUPABASE_KEY: str
    GROQ_API_KEY: str
    GOOGLE_API_KEY: str
    ELEVENLABS_API_KEY: str
    ELEVENLABS_VOICE_ID: str

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()