class STTHandler:
    """
    Handles Speech-To-Text (STT) transcription driven by LiveKit's VAD (Voice Activity Detection).
    This component transcribes user speech and feeds the resulting text to the Slow Reasoner.
    """
    def __init__(self, model_name: str = "whisper-large-v3-turbo"):
        # The specific STT model to be used (e.g., whisper-large-v3-turbo)
        self.model_name = model_name
        self._is_speech_active = False
        
    def on_speech_start(self):
        """
        Triggered by LiveKit VAD when user starts speaking.
        """
        self._is_speech_active = True
        # Logic to begin buffering or streaming to the STT provider

    def on_speech_end(self, audio_segment) -> str:
        """
        Triggered by LiveKit VAD when user stops speaking.
        Finalizes the transcription for this turn.
        
        Returns:
            The transcribed text to be fed into the Slow Reasoner.
        """
        self._is_speech_active = False
        # Logic to finalize transcription using self.model_name
        # return transcribed_text
        return ""
