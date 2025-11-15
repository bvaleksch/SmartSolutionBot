"""Auto-evaluation pipeline for the `test_competition` → `first_track` challenge.

The scorer unpacks contestant archives, launches their solution inside a temporary
Docker container, and computes a score based on the number of correct answers in
``output.csv`` with a small random bonus. The script enforces the competition rules:

* ``main.py`` must live in the archive root.
* ``input.csv`` is provided in the working directory.
* ``output.csv`` must be produced in the same root directory.

Any deviation (missing files, Docker errors, timeouts, etc.) is reflected in the
returned :class:`~smart_solution.bot.services.auto_judge.AutoJudgeResult`.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import random
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from smart_solution.db.enums import SubmissionStatus
from smart_solution.bot.services.auto_judge import auto_judge, AutoJudgeResult


logger = logging.getLogger(__name__)
DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "auto_judge" / "test_competition" / "first_track"
INPUT_FILE = DATA_ROOT / "input.csv"
DOCKER_IMAGE = "python:3.11-slim"
WORKDIR_CONTAINER = "/workspace"
OUTPUT_FILENAME = "output.csv"
EXEC_TIMEOUT = 120  # seconds


@dataclass(slots=True)
class _EvaluationOutcome:
	correct: int
	total: int
	value: float
	message: str


async def _score_submission(archive_path: Path) -> AutoJudgeResult:
	"""Evaluate a submission archive asynchronously inside a worker thread.

	The archive is unpacked to a temporary directory and executed in Docker.
	The returned :class:`AutoJudgeResult` captures both success and failure details.
	"""
	logger.info("Auto-judge first_track: evaluating %s", archive_path)
	if not INPUT_FILE.exists():
		logger.error("Auto-judge dataset missing at %s", INPUT_FILE)
		return AutoJudgeResult(
			status=SubmissionStatus.ERROR,
			value=None,
			success=False,
			message=f"Input dataset is missing at {INPUT_FILE}",
		)

	try:
		return await asyncio.to_thread(_evaluate_sync, archive_path)
	except Exception as exc:  # noqa: BLE001
		logger.exception("Auto-judge first_track failed during evaluation")
		return AutoJudgeResult(
			status=SubmissionStatus.ERROR,
			value=None,
			success=False,
			message=f"Auto evaluation failed: {exc}",
		)


def _evaluate_sync(archive_path: Path) -> AutoJudgeResult:
	"""Synchronously orchestrate the evaluation inside a temporary directory."""
	with TemporaryDirectory(prefix="auto-judge-") as tmp:
		tmp_dir = Path(tmp)
		try:
			main_path = _prepare_workspace(tmp_dir, archive_path)
		except FileNotFoundError as err:
			logger.error("Auto-judge first_track: %s", err)
			return AutoJudgeResult(
				status=SubmissionStatus.ERROR,
				value=None,
				success=False,
				message=str(err),
			)

		main_relative = main_path.relative_to(tmp_dir)
		logger.debug(
			"Auto-judge first_track: running container from %s with script %s",
			main_relative.parent,
			main_relative.name,
		)
		exec_result = _run_container(tmp_dir, main_relative)
		if not exec_result.success:
			logger.warning("Auto-judge first_track: container run failed: %s", exec_result.result.message)
			return exec_result.result

		output_file = tmp_dir / OUTPUT_FILENAME
		if not output_file.exists():
			logger.warning("Auto-judge first_track: output.csv missing, awarding 0 points")
			return AutoJudgeResult(
				status=SubmissionStatus.ACCEPTED,
				value=0.0,
				success=True,
				message="output.csv is missing — score 0.",
			)

		input_file = tmp_dir / "input.csv"
		if not input_file.exists():
			raise FileNotFoundError("Prepared input.csv disappeared during evaluation")

		outcome = _calculate_score(input_file, output_file)
		logger.info(
			"Auto-judge first_track: correct=%s total=%s value=%.3f",
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


def _prepare_workspace(tmp_dir: Path, archive_path: Path) -> Path:
	"""Unpack the submission archive and ensure ``main.py`` sits in the directory root."""
	if not archive_path.exists():
		raise FileNotFoundError(f"Archive {archive_path} not found")

	with zipfile.ZipFile(archive_path) as zf:
		zf.extractall(tmp_dir)

	entries = [p for p in tmp_dir.iterdir() if p.name != "__MACOSX"]
	if len(entries) == 1 and entries[0].is_dir():
		root = entries[0]
		logger.debug(
			"Auto-judge first_track: archive wrapped in folder '%s', flattening", root.name
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
		"Auto-judge first_track: extracted files: %s",
		", ".join(str(p.relative_to(tmp_dir)) for p in tmp_dir.iterdir()),
	)
	main_path = next((p for p in tmp_dir.rglob("main.py") if p.is_file()), None)
	if main_path is None:
		raise FileNotFoundError("main.py was not found inside the submission archive.")
	if main_path.parent != tmp_dir:
		raise FileNotFoundError("main.py must be located in the archive root.")

	# Ensure input is available both at root and alongside main.py
	root_input = tmp_dir / "input.csv"
	shutil.copy(INPUT_FILE, root_input)

	return main_path


@dataclass(slots=True)
class _ContainerExecResult:
	success: bool
	result: AutoJudgeResult


def _run_container(tmp_dir: Path, main_relative: Path) -> _ContainerExecResult:
	"""Execute ``python3 main.py input.csv`` inside Docker and capture the outcome."""
	main_rel_parts = main_relative.parts
	workdir_suffix = "/".join(main_rel_parts[:-1])
	workdir = WORKDIR_CONTAINER if not workdir_suffix else f"{WORKDIR_CONTAINER}/{workdir_suffix}"
	command_main = main_rel_parts[-1] if main_rel_parts else "main.py"
	main_exists = (tmp_dir / main_relative).exists()
	logger.debug(
		"Auto-judge first_track: container workdir=%s, command=%s, main_exists=%s",
		workdir,
		command_main,
		main_exists,
	)
	cmd = [
	    "docker",
	    "run",
	    "--rm",
	    "--network=none",
	    "-v", f"{tmp_dir}:{WORKDIR_CONTAINER}",
	    "-w", workdir,
	    DOCKER_IMAGE,
	    "python3",
	    command_main
	]
	try:
		completed = subprocess.run(
			cmd,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			check=False,
			text=True,
			timeout=EXEC_TIMEOUT,
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
	except subprocess.TimeoutExpired:
		logger.error("Auto-judge first_track timeout after %s seconds", EXEC_TIMEOUT)
		return _ContainerExecResult(
			success=False,
			result=AutoJudgeResult(
				status=SubmissionStatus.ERROR,
				value=None,
				success=False,
				message=f"Execution timed out after {EXEC_TIMEOUT}s.",
			),
		)

	if completed.returncode != 0:
		logger.warning(
			"Auto-judge first_track: container exited with %s, stderr=%s",
			completed.returncode,
			completed.stderr.strip(),
		)
		return _ContainerExecResult(
			success=False,
			result=AutoJudgeResult(
				status=SubmissionStatus.ERROR,
				value=None,
				success=False,
				message=f"Execution failed (exit {completed.returncode}). stderr: {completed.stderr.strip()}",
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


def _calculate_score(input_file: Path, output_file: Path) -> _EvaluationOutcome:
	"""Compare contestant predictions with the reference answers and build a score."""
	reference = _read_values(input_file)
	predictions = _read_values(output_file)

	correct = 0
	for item_id, num in reference.items():
		expected = num * num
		predicted = predictions.get(item_id)
		if predicted is None:
			continue
		try:
			pred_val = float(predicted)
		except ValueError:
			continue
		if abs(pred_val - expected) < 1e-6:
			correct += 1

	total = len(reference)
	random_bonus = random.random()
	value = float(correct + random_bonus)
	message = f"Correct: {correct}/{total}, bonus={random_bonus:.3f}"
	return _EvaluationOutcome(correct=correct, total=total, value=value, message=message)


def _read_values(csv_path: Path) -> dict[str, float]:
	"""Read ``id``/``num`` rows from a CSV file into a dictionary of floats."""
	records: dict[str, float] = {}
	with csv_path.open("r", newline="") as fh:
		reader = csv.DictReader(fh)
		if "id" not in reader.fieldnames or "num" not in reader.fieldnames:
			raise ValueError(f"CSV {csv_path} lacks required columns 'id' and 'num'")
		for row in reader:
			item_id = str(row["id"]).strip()
			value_raw: Optional[str] = row.get("num")
			if not item_id or value_raw is None:
				continue
			try:
				records[item_id] = float(value_raw)
			except ValueError:
				continue
	return records


@auto_judge.register("first_track")
async def score_first_track_submission(file_path: Path, submission, team, track):
	return await _score_submission(file_path)
