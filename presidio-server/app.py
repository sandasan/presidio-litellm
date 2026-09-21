import os

import yaml
from fastapi import FastAPI
from pydantic import BaseModel
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine

app = FastAPI()

NLP_CONFIG = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
}


def build_analyzer() -> AnalyzerEngine:
    provider = NlpEngineProvider(nlp_configuration=NLP_CONFIG)
    nlp_engine = provider.create_engine()
    registry = RecognizerRegistry()
    registry.supported_languages = ["en"]

    # Стандартные распознаватели (PERSON, EMAIL_ADDRESS, PHONE_NUMBER, ...)
    registry.load_predefined_recognizers(nlp_engine=nlp_engine)

    # Кастомные распознаватели из ANALYZER_CONF_FILE (API-ключи, БД, внутренние IP)
    conf_file = os.getenv("ANALYZER_CONF_FILE", "/app/presidio_config.yaml")
    try:
        with open(conf_file) as f:
            conf = yaml.safe_load(f) or {}
        for rec_conf in conf.get("recognizers", []):
            patterns = [
                Pattern(p.get("name", f"pattern-{i}"), p["regex"], float(p.get("score", 0.5)))
                for i, p in enumerate(rec_conf.get("patterns", []))
            ]
            recognizer = PatternRecognizer(
                name=rec_conf["name"],
                supported_language=rec_conf.get("supported_language", "en"),
                supported_entity=rec_conf.get("supported_entity", rec_conf.get("name", "GENERIC")),
                patterns=patterns,
                context=rec_conf.get("context"),
            )
            registry.add_recognizer(recognizer)
        print(f"[presidio] Загружено кастомных распознавателей: {len(conf.get('recognizers', []))}", flush=True)
    except Exception as e:
        print(f"[presidio] Не удалось загрузить ANALYZER_CONF_FILE={conf_file}: {e}", flush=True)

    return AnalyzerEngine(registry=registry, nlp_engine=nlp_engine)


analyzer = build_analyzer()
anonymizer = AnonymizerEngine()


class AnalyzeRequest(BaseModel):
    text: str
    language: str = "en"


class AnonymizeRequest(BaseModel):
    text: str
    analyzer_results: list


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    results = analyzer.analyze(text=req.text, language=req.language)
    return [r.to_dict() for r in results]


@app.post("/anonymize")
def anonymize(req: AnonymizeRequest):
    from presidio_analyzer import RecognizerResult

    recon_results = [
        RecognizerResult(
            entity_type=r.get("entity_type", "GENERIC"),
            start=int(r["start"]),
            end=int(r["end"]),
            score=float(r.get("score", 0.0)),
        )
        for r in req.analyzer_results
    ]
    result = anonymizer.anonymize(text=req.text, analyzer_results=recon_results)
    return {"text": result.text}