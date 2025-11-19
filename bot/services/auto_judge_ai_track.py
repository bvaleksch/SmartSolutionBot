# bot/services/auto_judge_ai_track.py

from __future__ import annotations

"""
Auto-evaluation pipeline for the `ai_track` challenge.

The scorer unpacks contestant archives, launches their solution inside a temporary
Docker container, and computes a score based on the quality of answers in
`output.csv` compared to reference answers in `dataset.csv`.

Key rules:

* `main.py` must live in the archive root.
* `input.csv` is provided by the organizers in the same root directory.
* The solution is executed with: `python3 main.py`.
* The script must produce `output.csv` in the same root directory with columns:
  - id
  - answer
  - documents  (required, but NOT validated for correctness)

Constraints for the container:

* Max execution time: 600 seconds.
* Max RAM: 10GB.
* No internet access (`--network none`).

The evaluation uses:
* Token-level F1 (NLTK-based tokenization and optional stemming).
* Semantic similarity via sentence embeddings (SentenceTransformer).

The final score is in [0, 100] and is averaged over all questions, so it does
not depend directly on the dataset size.
"""

import asyncio
import collections
import logging
import re
import shutil
import string
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

import numpy as np
import pandas as pd
from nltk.stem.snowball import SnowballStemmer
from nltk.tokenize import wordpunct_tokenize
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from smart_solution.db.enums import SubmissionStatus
from smart_solution.bot.services.auto_judge import auto_judge, AutoJudgeResult


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (paths, docker, limits)
# ---------------------------------------------------------------------------

DATA_ROOT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "auto_judge"
    / "ai_track"
)

# Hidden dataset with id, question, correct_answer (not exposed to participants)
DATASET_FILE = DATA_ROOT / "dataset.csv"

# Input questions that will be placed next to main.py as input.csv
# Columns: id, question
INPUT_FILE = DATA_ROOT / "input.csv"

# Name of the output file expected from the participant's code.
# Columns: id, answer, documents
OUTPUT_FILENAME = "output.csv"

# Docker image used to run participant submissions
DOCKER_IMAGE = "ai-track-runner:latest"  # replace with your actual image name if needed

WORKDIR_CONTAINER = "/workspace"
EXEC_TIMEOUT = 600  # seconds
MEMORY_LIMIT = "10g"  # 10GB RAM limit for the container

MAX_SCORE = 100.0

# Text processing configuration
_PUNCT_RE = re.compile(rf"[{re.escape(string.punctuation)}]")
_STEM_LANGUAGE = "russian"
_EMBEDDING_MODEL_NAME = "intfloat/multilingual-e5-base"

_embedding_model: Optional[SentenceTransformer] = None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _EvaluationOutcome:
    correct: int
    total: int
    value: float
    message: str


@dataclass(slots=True)
class _ContainerExecResult:
    success: bool
    result: AutoJudgeResult


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

@auto_judge.register("ai_track")
async def score_ai_track_submission(
    file_path: Path,
    submission,
    team,
    track,
) -> AutoJudgeResult:
    return await _score_submission(file_path)


async def _score_submission(archive_path: Path) -> AutoJudgeResult:
    """Evaluate a submission archive asynchronously inside a worker thread."""
    logger.info("Auto-judge ai_track: evaluating %s", archive_path)

    if not DATASET_FILE.exists():
        logger.error("AI track dataset missing at %s", DATASET_FILE)
        return AutoJudgeResult(
            status=SubmissionStatus.ERROR,
            value=None,
            success=False,
            message=f"Dataset file is missing at {DATASET_FILE}",
        )

    if not INPUT_FILE.exists():
        logger.error("AI track input file missing at %s", INPUT_FILE)
        return AutoJudgeResult(
            status=SubmissionStatus.ERROR,
            value=None,
            success=False,
            message=f"Input file is missing at {INPUT_FILE}",
        )

    try:
        return await asyncio.to_thread(_evaluate_sync, archive_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Auto-judge ai_track failed during evaluation")
        return AutoJudgeResult(
            status=SubmissionStatus.ERROR,
            value=None,
            success=False,
            message=f"Auto evaluation failed: {exc}",
        )


def _evaluate_sync(archive_path: Path) -> AutoJudgeResult:
    """Synchronously orchestrate the evaluation inside a temporary directory."""
    with TemporaryDirectory(prefix="auto-judge-ai-track-") as tmp:
        tmp_dir = Path(tmp)
        try:
            main_path = _prepare_workspace(tmp_dir, archive_path)
        except FileNotFoundError as err:
            logger.error("Auto-judge ai_track: %s", err)
            return AutoJudgeResult(
                status=SubmissionStatus.ERROR,
                value=None,
                success=False,
                message=str(err),
            )

        main_relative = main_path.relative_to(tmp_dir)
        logger.debug(
            "Auto-judge ai_track: running container from %s with script %s",
            main_relative.parent,
            main_relative.name,
        )
        exec_result = _run_container(tmp_dir, main_relative)
        if not exec_result.success:
            logger.warning(
                "Auto-judge ai_track: container run failed: %s",
                exec_result.result.message,
            )
            return exec_result.result

        output_file = tmp_dir / OUTPUT_FILENAME
        if not output_file.exists():
            logger.warning("Auto-judge ai_track: output.csv missing, awarding score=0")
            return AutoJudgeResult(
                status=SubmissionStatus.ACCEPTED,
                value=0.0,
                success=True,
                message="output.csv is missing — score 0.",
            )

        outcome = _calculate_score(DATASET_FILE, output_file)
        logger.info(
            "Auto-judge ai_track: approx_correct=%s total=%s value=%.3f",
            outcome.correct,
            outcome.total,
            outcome.value,
        )
        return AutoJudgeResult(
            status=SubmissionStatus.ACCEPTED,
            value=outcome.value,
            success=True,
            message=outcome.message,
        )


# ---------------------------------------------------------------------------
# Workspace preparation and container execution
# ---------------------------------------------------------------------------

def _prepare_workspace(tmp_dir: Path, archive_path: Path) -> Path:
    """Unpack the submission archive and ensure `main.py` sits in the directory root."""
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive {archive_path} not found")

    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(tmp_dir)

    entries = [p for p in tmp_dir.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        root = entries[0]
        logger.debug(
            "Auto-judge ai_track: archive wrapped in folder '%s', flattening",
            root.name,
        )
        for child in root.iterdir():
            target = tmp_dir / child.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(child), target)
        shutil.rmtree(root, ignore_errors=True)

    logger.debug(
        "Auto-judge ai_track: extracted files: %s",
        ", ".join(str(p.relative_to(tmp_dir)) for p in tmp_dir.iterdir()),
    )

    main_path = next((p for p in tmp_dir.rglob("main.py") if p.is_file()), None)
    if main_path is None:
        raise FileNotFoundError("main.py was not found inside the submission archive.")
    if main_path.parent != tmp_dir:
        raise FileNotFoundError("main.py must be located in the archive root.")

    # Provide input.csv to the container at the root directory
    root_input = tmp_dir / "input.csv"
    shutil.copy(INPUT_FILE, root_input)

    return main_path


def _run_container(tmp_dir: Path, main_relative: Path) -> _ContainerExecResult:
    """Execute `python3 main.py` inside Docker and capture the outcome."""
    main_rel_parts = main_relative.parts
    workdir_suffix = "/".join(main_rel_parts[:-1])
    workdir = WORKDIR_CONTAINER if not workdir_suffix else f"{WORKDIR_CONTAINER}/{workdir_suffix}"
    command_main = main_rel_parts[-1] if main_rel_parts else "main.py"
    main_exists = (tmp_dir / main_relative).exists()
    container_name = f"ai-track-{uuid.uuid4().hex}"
    logger.debug(
        "Auto-judge ai_track: container workdir=%s, command=%s, main_exists=%s",
        workdir,
        command_main,
        main_exists,
    )
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        "none",            # no internet access
        "--memory",
        MEMORY_LIMIT,      # RAM limit, e.g. 10g
        "--memory-swap",
        MEMORY_LIMIT,      # same as memory to disable swap extension
        "-v",
        f"{tmp_dir}:{WORKDIR_CONTAINER}",
        "-w",
        workdir,
        DOCKER_IMAGE,
        "python3",
        command_main,
    ]
    try:
        process = subprocess.Popen(
            docker_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        logger.error("Docker executable not found when evaluating submission")
        return _ContainerExecResult(
            success=False,
            result=AutoJudgeResult(
                status=SubmissionStatus.ERROR,
                value=None,
                success=False,
                message="Docker executable is not available.",
            ),
        )
    try:
        stdout, stderr = process.communicate(timeout=EXEC_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.error("Auto-judge ai_track timeout after %s seconds", EXEC_TIMEOUT)
        _kill_container(container_name)
        process.kill()
        stdout, stderr = process.communicate()
        return _ContainerExecResult(
            success=False,
            result=AutoJudgeResult(
                status=SubmissionStatus.ERROR,
                value=None,
                success=False,
                message=f"Execution timed out after {EXEC_TIMEOUT}s.",
            ),
        )

    if process.returncode != 0:
        logger.warning(
            "Auto-judge ai_track: container exited with %s, stderr=%s",
            process.returncode,
            (stderr or "").strip(),
        )
        return _ContainerExecResult(
            success=False,
            result=AutoJudgeResult(
                status=SubmissionStatus.ERROR,
                value=None,
                success=False,
                message=(
                    f"Execution failed (exit {process.returncode}). "
                    f"stderr: {(stderr or '').strip()}"
                ),
            ),
        )

    return _ContainerExecResult(
        success=True,
        result=AutoJudgeResult(
            status=SubmissionStatus.ACCEPTED,
            value=0.0,
            success=True,
        ),
    )


def _kill_container(container_name: str) -> None:
    """Force stop the docker container if it is still running."""
    try:
        completed = subprocess.run(
            ["docker", "kill", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            logger.warning(
                "Failed to kill container %s: exit=%s stderr=%s",
                container_name,
                completed.returncode,
                (completed.stderr or "").strip(),
            )
    except FileNotFoundError:
        logger.error("Docker executable not found while attempting to kill %s", container_name)
    except Exception:
        logger.exception("Unexpected error while killing container %s", container_name)


# ---------------------------------------------------------------------------
# Text processing and scoring helpers
# ---------------------------------------------------------------------------

def _get_stemmer() -> Optional[SnowballStemmer]:
    """Return a SnowballStemmer for the configured language if available."""
    try:
        return SnowballStemmer(_STEM_LANGUAGE)
    except ValueError:
        logger.warning(
            "SnowballStemmer does not support language '%s', disabling stemming",
            _STEM_LANGUAGE,
        )
        return None


def _normalize_text(text: str, stemmer: Optional[SnowballStemmer]) -> list[str]:
    """Lowercase, remove punctuation, tokenize and optionally stem."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)

    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in wordpunct_tokenize(text) if t.strip()]

    if stemmer is not None:
        tokens = [stemmer.stem(t) for t in tokens]

    return tokens


def _f1_from_tokens(gold_tokens: list[str], pred_tokens: list[str]) -> float:
    """Compute token-level F1 between gold and predicted token lists."""
    if not gold_tokens and not pred_tokens:
        return 1.0
    if not gold_tokens or not pred_tokens:
        return 0.0

    gold_counts = collections.Counter(gold_tokens)
    pred_counts = collections.Counter(pred_tokens)

    overlap = sum((gold_counts & pred_counts).values())
    if overlap == 0:
        return 0.0

    precision = overlap / len(pred_tokens)
    recall = overlap / len(gold_tokens)
    if precision + recall == 0:
        return 0.0

    return 2.0 * precision * recall / (precision + recall)


def _exact_match(gold_norm: str, pred_norm: str) -> int:
    """Return 1 if normalized answers are exactly equal, else 0."""
    return int(gold_norm == pred_norm)


def _build_normalized_string(tokens: list[str]) -> str:
    """Build a normalized string from tokens for EM comparison."""
    return " ".join(tokens)


def _get_embedding_model() -> Optional[SentenceTransformer]:
    """Lazily load and cache the embedding model."""
    global _embedding_model
    if _embedding_model is not None:
        return _embedding_model
    try:
        logger.info(
            "Loading SentenceTransformer embedding model '%s'",
            _EMBEDDING_MODEL_NAME,
        )
        _embedding_model = SentenceTransformer(_EMBEDDING_MODEL_NAME)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load embedding model: %s", exc)
        _embedding_model = None
    return _embedding_model


def _read_dataset(csv_path: Path) -> pd.DataFrame:
    """Read the dataset with gold answers."""
    df = pd.read_csv(csv_path)
    required = {"id", "question", "correct_answer"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Dataset CSV {csv_path} lacks required columns: {', '.join(sorted(missing))}"
        )
    # Cast id to string for robust joining
    df["id"] = df["id"].astype(str).str.strip()
    return df[["id", "question", "correct_answer"]]


def _read_predictions(csv_path: Path) -> pd.DataFrame:
    """
    Read contestant predictions (output.csv).

    Expected columns:
    - id
    - answer
    - documents (required but ignored for scoring)
    """
    df = pd.read_csv(csv_path)
    required = {"id", "answer", "documents"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Output CSV {csv_path} lacks required columns: {', '.join(sorted(missing))}"
        )
    df["id"] = df["id"].astype(str).str.strip()
    # We keep 'documents' only to enforce its presence; it is not used further.
    return df[["id", "answer", "documents"]]


def _compute_metrics(merged: pd.DataFrame) -> dict[str, float]:
    """Compute lexical F1, semantic similarity and combined score."""
    stemmer = _get_stemmer()

    gold_tokens_list: list[list[str]] = []
    pred_tokens_list: list[list[str]] = []
    gold_norm_strings: list[str] = []
    pred_norm_strings: list[str] = []

    for _, row in merged.iterrows():
        gold_text = row["correct_answer"]
        pred_text = row["answer"]

        gold_tokens = _normalize_text(gold_text, stemmer)
        pred_tokens = _normalize_text(pred_text, stemmer)

        gold_tokens_list.append(gold_tokens)
        pred_tokens_list.append(pred_tokens)

        gold_norm_strings.append(_build_normalized_string(gold_tokens))
        pred_norm_strings.append(_build_normalized_string(pred_tokens))

    f1_values: list[float] = []
    em_values: list[int] = []

    for gold_tokens, pred_tokens, gold_norm, pred_norm in zip(
        gold_tokens_list,
        pred_tokens_list,
        gold_norm_strings,
        pred_norm_strings,
    ):
        f1_values.append(_f1_from_tokens(gold_tokens, pred_tokens))
        em_values.append(_exact_match(gold_norm, pred_norm))

    mean_f1 = float(np.mean(f1_values)) if f1_values else 0.0
    mean_em = float(np.mean(em_values)) if em_values else 0.0

    # Semantic similarity
    model = _get_embedding_model()
    if model is None:
        logger.warning(
            "Embedding model is not available, semantic similarity will be 0.0"
        )
        mean_semantic = 0.0
    else:
        gold_texts = merged["correct_answer"].astype(str).tolist()
        pred_texts = merged["answer"].astype(str).tolist()

        gold_embeddings = model.encode(
            gold_texts,
            batch_size=64,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        pred_embeddings = model.encode(
            pred_texts,
            batch_size=64,
            show_progress_bar=False,
            normalize_embeddings=True,
        )

        cos_matrix = cosine_similarity(gold_embeddings, pred_embeddings)
        cos_diag = np.diag(cos_matrix)
        semantic_scores = np.clip(cos_diag, 0.0, 1.0)
        mean_semantic = float(np.mean(semantic_scores)) if len(semantic_scores) else 0.0

    combined_0_1 = 0.7 * mean_f1 + 0.3 * mean_semantic
    combined_0_1 = float(np.clip(combined_0_1, 0.0, 1.0))
    final_score = combined_0_1 * MAX_SCORE

    return {
        "mean_f1": mean_f1,
        "mean_semantic": mean_semantic,
        "mean_em": mean_em,
        "score_0_100": final_score,
    }


def _calculate_score(dataset_file: Path, output_file: Path) -> _EvaluationOutcome:
    """Compare contestant predictions with the reference answers and build a score."""
    dataset_df = _read_dataset(dataset_file)
    pred_df = _read_predictions(output_file)

    merged = dataset_df.merge(pred_df, on="id", how="left")
    # Empty answers for missing ids are treated as empty strings → F1 = 0, semantic = 0.
    merged["answer"] = merged["answer"].fillna("")

    metrics = _compute_metrics(merged)

    total = len(merged)
    approx_correct = int(round(metrics["mean_f1"] * total))
    value = float(metrics["score_0_100"])

    message = (
        f"Mean F1={metrics['mean_f1']:.4f}, "
        f"Mean semantic={metrics['mean_semantic']:.4f}, "
        f"Mean EM={metrics['mean_em']:.4f}, "
        f"Final score={value:.2f}"
    )

    return _EvaluationOutcome(
        correct=approx_correct,
        total=total,
        value=value,
        message=message,
    )
