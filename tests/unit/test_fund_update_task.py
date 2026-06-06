from src.services.fund_update_task import FundUpdateTask


def test_start_rejects_second_run_while_running():
    task = FundUpdateTask()

    first = task.start(lambda: {"success": True, "message": "ok"}, run_inline=True, keep_running=True)
    second = task.start(lambda: {"success": True}, run_inline=True)

    assert first["success"] is True
    assert first["data"]["in_progress"] is True
    assert second["success"] is False
    assert "正在进行" in second["message"]


def test_status_records_success_after_inline_run():
    task = FundUpdateTask()

    task.start(lambda: {"success": True, "message": "同步完成"}, run_inline=True)
    status = task.status()

    assert status["in_progress"] is False
    assert status["last_result"]["success"] is True
    assert status["last_result"]["message"] == "同步完成"
