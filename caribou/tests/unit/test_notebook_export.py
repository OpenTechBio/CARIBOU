from caribou.core.io_helpers import chat_history_to_notebook


def test_chat_history_to_notebook_includes_user_prompt_and_assistant_code():
    notebook = chat_history_to_notebook([
        {"role": "system", "content": "hidden"},
        {"role": "user", "content": "Please load the data"},
        {
            "role": "assistant",
            "content": "I will load it.\n\n```python\nimport scanpy as sc\nadata = sc.read_h5ad('/workspace/dataset.h5ad')\n```",
        },
        {"role": "user", "content": "Code execution result:\n[status: ok]"},
    ])

    cells = notebook["cells"]
    assert cells[0]["cell_type"] == "markdown"
    assert cells[0]["source"] == "**User prompt:**\n\nPlease load the data"
    assert cells[1]["cell_type"] == "markdown"
    assert cells[1]["source"] == "I will load it."
    assert cells[2]["cell_type"] == "code"
    assert "sc.read_h5ad" in cells[2]["source"]
    assert len(cells) == 3
