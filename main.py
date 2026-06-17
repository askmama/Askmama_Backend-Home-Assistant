from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.iot.mqtt_client import start_mqtt
from app.api.routes import router

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_mqtt()
    yield

app = FastAPI(title="AskMama API", lifespan=lifespan)
app.include_router(router)

@app.get("/")
def health():
    return {"status": "AskMama backend running"}

##python mama_voice.py