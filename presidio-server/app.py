import os
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine, RecognizerResult
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
recognizer_config = os.getenv("ANALYZER_CONF_FILE")
if recognizer_config:
    analyzer.registry.add_recognizers_from_yaml(recognizer_config)
    print(f"Loaded custom recognizers from {recognizer_config}", flush=True)
anonymizer = AnonymizerEngine()
web_url_pattern = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
url_host_entities = {"URL", "IP_ADDRESS", "INTERNAL_IP", "INTERNAL_HOST"}
# --------------------------------------------------

class SanitizeRequest(BaseModel):
    text: str
    language: str = "en"

class AnalyzeRequest(BaseModel):
    text: str
    language: str = "en"
    entities: list[str] | None = None

class AnonymizeRequest(BaseModel):
    text: str
    analyzer_results: list[dict]

def analyze_text(text: str, language: str, entities: list[str] | None = None):
    results = analyzer.analyze(text=text, language=language, entities=entities)
    url_spans = [match.span() for match in web_url_pattern.finditer(text)]
    other_entity_spans = [
        (result.start, result.end)
        for result in results
        if result.entity_type != "URL"
    ]
    results = [
        result
        for result in results
        if not (
            result.entity_type in url_host_entities
            and any(result.start < end and result.end > start for start, end in url_spans)
        )
        and not (
            result.entity_type == "URL"
            and (
                any(result.start < end and result.end > start for start, end in url_spans)
                or any(result.start < end and result.end > start for start, end in other_entity_spans)
            )
        )
    ]
    return results

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    try:
        results = analyze_text(req.text, req.language, req.entities)
        return [
            {
                "entity_type": result.entity_type,
                "start": result.start,
                "end": result.end,
                "score": result.score,
            }
            for result in results
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@app.post("/anonymize")
def anonymize(req: AnonymizeRequest):
    try:
        results = [
            RecognizerResult(
                entity_type=result["entity_type"],
                start=result["start"],
                end=result["end"],
                score=result["score"],
            )
            for result in req.analyzer_results
        ]
        result = anonymizer.anonymize(text=req.text, analyzer_results=results)
        return {"text": result.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@app.post("/")
def sanitize_text(req: SanitizeRequest):
    try:
        analysis_results = analyze_text(req.text, req.language)
        anonymized_result = anonymizer.anonymize(text=req.text, analyzer_results=analysis_results)
        return {"text": anonymized_result.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
