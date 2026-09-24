from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine

app = FastAPI()

# --- КРИТИЧЕСКАЯ НАСТРОЙКА МУЛЬТИЯЗЫЧНОГО ДВИЖКА ---
# Создаем конфигурацию для одновременной загрузки en, ru и uk моделей spacy
nlp_configuration = {
    "nlp_engine_name": "spacy",
    "models": [
        {"lang_code": "en", "model_name": "en_core_web_lg"},
        {"lang_code": "ru", "model_name": "ru_core_news_lg"},
        {"lang_code": "uk", "model_name": "uk_core_news_lg"}  # Регистрируем украинскую модель
    ]
}

# Инициализируем NLP движок на основе нашей конфигурации
provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
nlp_engine = provider.create_engine()

# Передаем мультиязычный nlp_engine в анализатор Presidio
analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en", "ru", "uk"])
anonymizer = AnonymizerEngine()
# --------------------------------------------------

class SanitizeRequest(BaseModel):
    text: str
    language: str = "en"

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/")
def sanitize_text(req: SanitizeRequest):
    try:
        # Presidio автоматически выберет нужную модель на основе переданного языка
        analysis_results = analyzer.analyze(text=req.text, language=req.language)
        anonymized_result = anonymizer.anonymize(text=req.text, analyzer_results=analysis_results)
        return {"text": anonymized_result.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
