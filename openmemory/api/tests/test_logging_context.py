"""Tests for structured logging context (task_07)."""

import logging
import re

from app.utils.logging_context import (
    LOG_DATE_FORMAT,
    LOG_FORMAT,
    StructuredContextFilter,
    job_id_var,
    request_id_var,
)


class TestStructuredContext:
    def test_filter_injects_request_and_job_ids(self):
        filt = StructuredContextFilter()
        request_id_var.set("req-abc")
        job_id_var.set("job-xyz")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        assert filt.filter(record) is True
        assert record.request_id == "req-abc"
        assert record.job_id == "job-xyz"
        request_id_var.set("")
        job_id_var.set("")

    def test_filter_defaults_when_unset(self):
        filt = StructuredContextFilter()
        request_id_var.set("")
        job_id_var.set("")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        filt.filter(record)
        assert record.request_id == "-"
        assert record.job_id == "-"


class TestProcessLogFormat:
    def test_line_has_local_timestamp_and_keeps_level_name_prefix(self):
        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        record = logging.LogRecord(
            name="app.workers.write_worker",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="write worker started",
            args=(),
            exc_info=None,
        )

        line = formatter.format(record)

        assert re.match(
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4} "
            r"INFO:app\.workers\.write_worker:write worker started$",
            line,
        )
