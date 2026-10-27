from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_user_mode_frontend_reconciles_stopped_runs_instead_of_hanging_spinner() -> None:
    hook = (PROJECT_ROOT / "apps" / "renderer" / "lib" / "hooks" / "useChat.ts").read_text(encoding="utf-8")
    workspace = (PROJECT_ROOT / "apps" / "renderer" / "components" / "lumen" / "LumenWorkspace.tsx").read_text(encoding="utf-8")

    assert "STOPPED_RUN_STATES" in hook
    assert "reconcileRunState" in hook
    assert "reconcilePersistedThreadState" in hook
    assert "active_run_id" in hook
    assert "api.chatThreads()" in hook
    assert "bindActiveRun" in hook
    assert "meta.application_request" in hook
    assert "run_id" in hook
    assert "client_status" in hook
    assert "loading" in hook
    assert "chat.reconcileRunState(active.run)" in workspace


def test_user_mode_chat_clears_sending_for_waiting_question_before_loading_replacement() -> None:
    hook = (PROJECT_ROOT / "apps" / "renderer" / "lib" / "hooks" / "useChat.ts").read_text(encoding="utf-8")

    assert "CHAT_SEND_TIMEOUT_MS" in hook
    assert "controller.abort()" in hook
    assert "clearSendingForEpoch(requestEpoch)" in hook
    waiting_clear = hook.index('if (state === "WAITING_FOR_USER" || refreshedQuestion)')
    loading_return = hook.index("if (!hasLoading) return;")
    assert waiting_clear < loading_return
    assert "sendingRef.current = false;" in hook[waiting_clear:loading_return]
    assert "setSending(false);" in hook[waiting_clear:loading_return]


def test_user_mode_pending_question_card_renders_schema_details() -> None:
    card = (PROJECT_ROOT / "apps" / "renderer" / "components" / "lumen" / "chat" / "PendingQuestionCard.tsx").read_text(encoding="utf-8")

    assert "question.expected_answer_schema?.fields" in card
    assert "QuestionHeading" in card
    assert "schemaString(question, \"display_title\")" in card
    assert "display_reason" in card
    assert "question_contract_valid" in card
    assert "InvalidQuestionContract" in card
    assert "Re-reading the field" in card
    assert "!fieldOptions(field).length" in card
    assert "isChoiceField" in card
    # A required field can be left for the user; an optional one can be skipped.
    assert "Leave this one for me" in card
    assert "Skip it" in card
    assert "fieldOptions" in card
    assert "storedContext" in card
    assert "One of:" in card
    assert "Saved answer that may fit:" in card
    # The final-action card names the exact control and the payment summary, and carries the token.
    assert "contract.approval_token" in card
    assert "contract.submit_label" in card
    assert "contract.payment_floor === true" in card


def test_user_mode_frontend_starts_in_new_chat_session_without_restore_landing() -> None:
    hook = (PROJECT_ROOT / "apps" / "renderer" / "lib" / "hooks" / "useChat.ts").read_text(encoding="utf-8")

    # Fresh loads start on a new chat: no restore landing, no automatic thread.
    assert "const [threadId, setThreadId] = useState<number | null>(null)" in hook
    assert "discoverRestorableRuns" not in hook
    assert "RESTORABLE_RUN_STATES" not in hook
    assert "api.runs()" in hook
    assert "startPersistedRunBindingPoll" in hook
    assert "window.setInterval" in hook
    assert "const loadThread = useCallback" in hook
    assert "await adoptPersistedThread(thread)" in hook
    assert "if (thread.run_id)" in hook
    assert "bindActiveRun(thread.run_id)" in hook
    assert "meta.application_request" in hook
    assert "reconcilePersistedThreadState(tid)" in hook
    assert "stopPersistedRunBindingPoll?.()" in hook
    workspace = (PROJECT_ROOT / "apps" / "renderer" / "components" / "lumen" / "LumenWorkspace.tsx").read_text(encoding="utf-8")
    home = (PROJECT_ROOT / "apps" / "renderer" / "components" / "lumen" / "home" / "HomeScreen.tsx").read_text(encoding="utf-8")
    assert "const effectiveRunId = chat.sensitiveTakeover?.runId ?? chat.activeRunId" in workspace
    assert "const isHome = !hasMessages && chat.threadId == null && effectiveRunId == null && !sensitiveTakeoverActive" in workspace
    assert "What should I work on?" in home
    # Waiting runs surface as a list the user opens, never as an automatic restore.
    assert "Waiting on you" in home


def test_user_mode_sidebar_can_collapse_and_reopen() -> None:
    workspace = (PROJECT_ROOT / "apps" / "renderer" / "components" / "lumen" / "LumenWorkspace.tsx").read_text(encoding="utf-8")
    sidebar = (PROJECT_ROOT / "apps" / "renderer" / "components" / "lumen" / "shell" / "Sidebar.tsx").read_text(encoding="utf-8")

    assert "const [collapsed, toggleSidebar] = useSidebarCollapsed()" in workspace
    assert "collapsed={collapsed || narrow}" in workspace
    assert "onToggle={toggleNav}" in workspace
    # A narrow window keeps the rail and opens the full sidebar as an overlay.
    assert 'useMediaQuery("(max-width: 899px)")' in workspace
    assert '"lumen.sidebar"' in workspace
    assert "collapsed: boolean" in sidebar
    assert "onToggle: () => void" in sidebar
    assert "Expand sidebar" in sidebar
    assert "Collapse sidebar" in sidebar
    assert "PanelLeftOpen" in sidebar
    assert "PanelLeftClose" in sidebar
    assert "LumenWordmark" in sidebar
