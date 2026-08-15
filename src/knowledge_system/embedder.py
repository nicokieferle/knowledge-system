from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer


class LocalEmbedder:
    def __init__(self, model_name: str, expected_dimensions: int) -> None:
        print(f"[embedder] Loading model: {model_name}")
        self.model_name = model_name
        self.expected_dimensions = expected_dimensions
        self.model = SentenceTransformer(model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.expected_dimensions), dtype=np.float32)

        vectors = self.model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=len(texts) > 16,
        ).astype(np.float32)

        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        actual_dimensions = int(vectors.shape[1])
        if actual_dimensions != self.expected_dimensions:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {self.expected_dimensions}, "
                f"got {actual_dimensions} from {self.model_name}"
            )

        return vectors
