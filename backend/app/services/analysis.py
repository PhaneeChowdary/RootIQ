import json
from urllib import error, request

from ..models import RunbookRecommendation, SupportAnalysis, SupportCase


class AnalysisUnavailable(RuntimeError):
    pass


class OllamaSupportAnalyst:
    def __init__(self, url: str, model: str, chat_factory=None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        # Gives the chat model for each request: the main model with the local backup, or None for Ollama only.
        # A factory, not a model: a change on the Models page applies at the next request.
        self.chat_factory = chat_factory

    def analyze(self, case: SupportCase) -> SupportAnalysis:
        prompt = f"""You are a senior production support engineer. Analyze the customer case.
Return only valid JSON using this exact schema:
{{
  "incident_summary": "brief internal summary",
  "customer_impact": "impact in clear language",
  "likely_cause": "evidence-based probable cause or unknown",
  "suggested_severity": "critical|high|medium|low",
  "diagnostic_steps": ["specific next check"],
  "workaround": "safe temporary workaround or state none known",
  "information_to_request": ["missing information"],
  "customer_email_subject": "status update subject",
  "customer_email_body": "professional customer-facing update without unsupported claims"
}}

Case title: {case.title}
Customer: {case.customer}
Service: {case.service}
Environment: {case.environment}
Reported issue:
{case.reported_issue}

Logs/evidence:
{case.logs or "No logs provided."}
"""
        result = self._generate(prompt)
        try:
            return SupportAnalysis.model_validate_json(result["response"])
        except (KeyError, ValueError) as exc:
            raise AnalysisUnavailable("Model returned invalid analysis output.") from exc

    def recommend_runbook(self, case: SupportCase, resolved_cases: list[SupportCase]) -> RunbookRecommendation:
        evidence = "\n\n".join(
            f"Resolved case: {item.title}\n"
            f"Service: {item.service}\n"
            f"Cause: {item.analysis.likely_cause}\n"
            f"Resolution: {item.resolution}\n"
            f"Preventive actions: {', '.join(item.preventive_actions) or 'None recorded'}"
            for item in resolved_cases
        )
        prompt = f"""You are a production support lead drafting a safe investigation runbook.
Use only the resolved-case evidence supplied below. Do not claim a cause is confirmed for the current incident.
Return only valid JSON using this exact schema:
{{
  "title": "runbook title",
  "symptoms": ["symptom pattern to verify"],
  "verification_steps": ["specific diagnostic check"],
  "mitigation_steps": ["safe mitigation based on prior resolutions"],
  "escalation_guidance": "when to escalate or what to capture"
}}

Current incident:
Title: {case.title}
Service: {case.service}
Symptoms: {case.reported_issue}
Logs: {case.logs or "No logs provided."}

Resolved incident evidence:
{evidence or "No prior resolved incident evidence is available."}
"""
        result = self._generate(prompt)
        try:
            rec = RunbookRecommendation.model_validate_json(result["response"])
            return rec.model_copy(update={"evidence_case_ids": [c.id for c in resolved_cases]})
        except (KeyError, ValueError) as exc:
            raise AnalysisUnavailable("Model returned invalid runbook output.") from exc

    def _generate(self, prompt: str) -> dict:
        chat = self.chat_factory() if self.chat_factory else None
        if chat is not None:
            try:
                return {"response": chat.invoke(prompt).text}  # .text joins the content parts of Gemini 3
            except Exception as exc:  # both the main model and the backup failed
                raise AnalysisUnavailable("No model is available. Check the Models page and Ollama.") from exc
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False, "format": "json"}).encode()
        req = request.Request(
            f"{self.url}/api/generate", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except error.URLError as exc:
            raise AnalysisUnavailable("Ollama is not reachable. Is it running?") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise AnalysisUnavailable("Ollama returned unparseable output.") from exc
