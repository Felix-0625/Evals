from types import SimpleNamespace

import anyio
import pyarrow as pa
import pyarrow.parquet as pq
from inspect_ai._eval.task.scan import _cleanup_orphan_scan_rows


def test_orphan_scan_cleanup_preserves_live_rows_and_releases_reader(tmp_path, monkeypatch):
    async def summaries(path):
        return [SimpleNamespace(uuid="live")]

    monkeypatch.setattr(
        "inspect_ai.log._file.list_eval_logs", lambda path: [SimpleNamespace(name="current.eval")]
    )
    monkeypatch.setattr("inspect_ai.log._file.read_eval_log_sample_summaries_async", summaries)
    target = tmp_path / "findings.parquet"
    pq.write_table(pa.table({"transcript_id": ["live", "orphan"], "value": [1, 2]}), target)
    anyio.run(_cleanup_orphan_scan_rows, str(tmp_path), str(tmp_path), ["findings"])
    assert pq.read_table(target).to_pydict() == {"transcript_id": ["live"], "value": [1]}
    assert not target.with_suffix(".parquet.tmp").exists()
