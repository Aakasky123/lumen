from app.agent_eval.benchmarks import (
    WEBARENA_DOCKER_IMAGE,
    benchmark_targets,
    extract_json_payload,
    load_records,
    normalize_browsecomp_records,
    normalize_clawbench_records,
    normalize_mind2web_records,
    normalize_online_mind2web_records,
    normalize_webbench_records,
    normalize_webarena_agent_inputs,
    normalize_webvoyager_records,
    task_ids_from_webarena_subset,
    webarena_docker_command,
)


def test_extract_json_payload_skips_webarena_log_lines() -> None:
    text = """[WebArena Verified] [INFO] Loading tasks
[
  {"task_id": 108, "intent": "Find the monthly count", "sites": ["shopping_admin"]}
]
Unable to find image locally
"""
    payload = extract_json_payload(text)
    assert payload[0]["task_id"] == 108


def test_normalize_webarena_agent_inputs_to_lumen_manifest() -> None:
    tasks = normalize_webarena_agent_inputs(
        [
            {
                "task_id": 108,
                "intent": "Get the monthly count of completed orders.",
                "sites": ["shopping_admin"],
                "start_urls": ["http://localhost:8080/admin"],
                "task_type": "RETRIEVE",
            }
        ]
    )
    assert tasks[0].benchmark == "webarena-verified"
    assert tasks[0].external_id == "108"
    assert tasks[0].family == "webarena:shopping_admin"
    assert tasks[0].goal.startswith("Get the monthly count")
    assert tasks[0].operation == "RETRIEVE"


def test_normalize_mind2web_records_to_next_action_manifest() -> None:
    tasks = normalize_mind2web_records(
        [
            {
                "annotation_id": "abc",
                "confirmed_task": "Book a refundable hotel room.",
                "website": "travel.example",
                "actions": [{"operation": "CLICK"}],
            }
        ]
    )
    assert tasks[0].benchmark == "mind2web"
    assert tasks[0].family == "mind2web:next_action"
    assert tasks[0].operation == "next_action"
    assert tasks[0].sites == ["travel.example"]


def test_benchmark_targets_keep_end_to_end_and_static_datasets_separate() -> None:
    targets = {target.name: target for target in benchmark_targets()}
    assert targets["webarena-verified"].mode == "end_to_end_offline_environment"
    assert targets["mind2web"].mode == "static_next_action_dataset"
    assert targets["webbench"].mode == "live_read_write_web_benchmark"
    assert targets["webvoyager"].mode == "live_multisite_web_task_benchmark"
    assert targets["online-mind2web"].mode == "live_web_task_benchmark"
    assert targets["browsecomp"].mode == "research_answer_benchmark"
    assert targets["clawbench"].mode == "live_stateful_workflow_benchmark"


def test_normalize_online_mind2web_trajectory_to_live_task_manifest() -> None:
    tasks = normalize_online_mind2web_records(
        {
            "schema_version": "online-mind2web-v2",
            "task_id": "abc",
            "task": "Display the unemployment comparison figure.",
            "reference_length": 12,
            "action_history": [
                {"step": 0, "url": "https://www.americashealthrankings.org/"},
                {"step": 1, "url": "https://www.americashealthrankings.org/explore"},
            ],
        }
    )
    assert tasks[0].benchmark == "online-mind2web"
    assert tasks[0].start_urls == ["https://www.americashealthrankings.org/"]
    assert tasks[0].sites == ["www.americashealthrankings.org"]
    assert tasks[0].source["action_count"] == 2


def test_normalize_webbench_csv_row_to_live_read_write_manifest() -> None:
    tasks = normalize_webbench_records(
        [
            {
                "ID": "3",
                "Starting URL": "https://www.agoda.com",
                "Category": "CREATE",
                "Task": "Create a new wishlist titled Summer Getaways.",
            }
        ]
    )
    assert tasks[0].benchmark == "webbench"
    assert tasks[0].family == "webbench:write"
    assert tasks[0].operation == "WRITE"
    assert tasks[0].sites == ["www.agoda.com"]


def test_normalize_webvoyager_jsonl_row_to_live_task_manifest() -> None:
    tasks = normalize_webvoyager_records(
        [
            {
                "web_name": "Allrecipes",
                "id": "Allrecipes--0",
                "ques": "Provide a vegetarian lasagna recipe with more than 100 reviews.",
                "web": "https://www.allrecipes.com/",
            }
        ]
    )
    assert tasks[0].benchmark == "webvoyager"
    assert tasks[0].family == "webvoyager:allrecipes"
    assert tasks[0].sites == ["www.allrecipes.com"]
    assert tasks[0].operation == "live_web_task"


def test_normalize_browsecomp_row_to_research_answer_manifest() -> None:
    tasks = normalize_browsecomp_records(
        [{"id": "q1", "question": "Which archived page first mentions the product?", "answer": "Example"}]
    )
    assert tasks[0].benchmark == "browsecomp"
    assert tasks[0].family == "browsecomp:research_qa"
    assert tasks[0].operation == "research_answer"
    assert tasks[0].source["has_reference_answer"] is True


def test_normalize_clawbench_task_json_to_stateful_workflow_manifest() -> None:
    tasks = normalize_clawbench_records(
        {
            "metadata": {
                "task_id": 1,
                "metaclass": "daily-life",
                "class": "food",
                "description": "On Uber Eats, order delivery.",
                "sites_involved": ["ubereats.com"],
                "platform": "uber-eats",
            },
            "instruction": 'On Uber Eats, order delivery: one Pad Thai, note "no peanuts"',
            "eval_schema": {"url_pattern": "__PLACEHOLDER_WILL_NOT_MATCH__", "method": "POST"},
            "time_limit": 30,
        }
    )
    assert tasks[0].benchmark == "clawbench"
    assert tasks[0].family == "clawbench:daily-life:food"
    assert tasks[0].operation == "state_change"
    assert tasks[0].sites == ["ubereats.com"]


def test_load_records_accepts_csv_and_jsonl(tmp_path) -> None:
    csv_path = tmp_path / "webbench.csv"
    csv_path.write_text("ID,Starting URL,Category,Task\n1,https://example.com,READ,Find the price\n", encoding="utf-8")
    jsonl_path = tmp_path / "webvoyager.jsonl"
    jsonl_path.write_text('{"id":"a","ques":"Find a thing","web":"https://example.org"}\n', encoding="utf-8")
    assert load_records(csv_path)[0]["ID"] == "1"
    assert load_records(jsonl_path)[0]["ques"] == "Find a thing"


def test_webarena_docker_command_is_optional_external_wrapper() -> None:
    command = webarena_docker_command(["agent-input-get", "--task-ids", "108"])
    assert command[:4] == ["docker", "run", "--rm", WEBARENA_DOCKER_IMAGE]
    assert command[-2:] == ["--task-ids", "108"]


def test_webarena_docker_command_supports_host_mounts(tmp_path) -> None:
    command = webarena_docker_command(["subset-export"], mounts={tmp_path: "/data"})
    assert "-v" in command
    assert f"{tmp_path.resolve().as_posix()}:/data" in command
    assert command[-2:] == [WEBARENA_DOCKER_IMAGE, "subset-export"]


def test_task_ids_from_webarena_subset_keeps_stable_subset_order() -> None:
    ids = task_ids_from_webarena_subset(
        {"tasks": [{"task_id": 108}, {"id": "204"}, {"task_id": 999}]},
        limit=2,
    )
    assert ids == ["108", "204"]
