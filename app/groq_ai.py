import httpx
from app.config import settings

_URL = "https://api.groq.com/openai/v1/chat/completions"
_SYSTEM = (
    "You are a concise email/link tracking analytics assistant. "
    "Answer in 2–4 sentences using specific numbers from the data. "
    "Do not repeat the raw data back verbatim."
)


async def ask(question: str, context: str) -> str:
    if not settings.groq_api_key:
        return "Set GROQ_API_KEY in Railway Variables to enable AI analysis."
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            _URL,
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": f"Tracking data:\n{context}\n\nQuestion: {question}"},
                ],
                "max_tokens": 512,
                "temperature": 0.2,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
