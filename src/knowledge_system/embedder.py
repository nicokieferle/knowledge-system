from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer


class LocalEmbedder:
    def __init__(self, model_name: str, expected_dimensions: int) -> None:
        print(f"[embedder] Loading model: {model_name}")
        self.model_name = model_name
        self.expected_dimensions = expected_dimensions
        self.model = SentenceTransformer(model_name)
        if hasattr(self.model, "get_embedding_dimension"):
            actual_dimensions = self.model.get_embedding_dimension()
        else:
            actual_dimensions = self.model.get_sentence_embedding_dimension()
        print(f"[embedder] Model ready: dimensions={actual_dimensions}")

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.expected_dimensions), dtype=np.float32)

        print(f"[embedder] Encoding {len(texts)} text(s)")
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

        print(f"[embedder] Encoded {len(texts)} text(s), dimensions={actual_dimensions}")
        return vectors
