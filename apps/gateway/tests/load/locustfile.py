"""Locust stress test for OmniVoice API."""

from locust import HttpUser, between, task


class TTSUser(HttpUser):
    """Simulates a user making TTS requests."""

    wait_time = between(1, 3)

    @task(5)
    def synthesize_stock(self):
        """POST /api/v1/tts with stock voice."""
        self.client.post(
            "/api/v1/tts",
            json={
                "text": "Hola, esta es una prueba de estrés del sistema de síntesis de voz.",
                "voice_id": "es-mx-male",
                "language": "es",
                "speed": 1.0,
            },
            name="/api/v1/tts [stock]",
        )

    @task(2)
    def synthesize_instruct(self):
        """POST /api/v1/tts/instruct with custom instruct."""
        self.client.post(
            "/api/v1/tts/instruct",
            json={
                "text": "This is a stress test of the voice synthesis system.",
                "instruct": "female, young adult, american accent",
                "language": "en",
                "speed": 1.0,
            },
            name="/api/v1/tts/instruct",
        )

    @task(1)
    def list_voices(self):
        """GET /api/v1/voices/stock."""
        self.client.get("/api/v1/voices/stock", name="/api/v1/voices/stock")

    @task(1)
    def health_check(self):
        """GET /api/v1/health."""
        self.client.get("/api/v1/health", name="/api/v1/health")
