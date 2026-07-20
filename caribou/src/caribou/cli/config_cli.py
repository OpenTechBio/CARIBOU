# caribou/cli/config_cli.py
import os
import re
import typer
from rich.console import Console
from typing import Optional

# Import the centralized ENV_FILE path
from caribou.config import ENV_FILE
from caribou.core.control_access import resolve_control_access_token

config_app = typer.Typer(
    name="config",
    help="Manage CARIBOU configuration and API keys.",
    no_args_is_help=True,
)

console = Console()


@config_app.command("get-control-token")
def get_control_token(
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print only the token, for copying or command substitution.",
    ),
) -> None:
    """Show the persistent token used by the experiment web interface."""

    resolved = resolve_control_access_token(create=True, env_file=ENV_FILE)
    if resolved is None:  # Defensive: create=True always returns a token.
        raise typer.Exit(1)
    if raw:
        typer.echo(resolved.value)
        return

    source = {
        "environment": "CARIBOU_CONTROL_API_TOKEN in the current environment",
        "caribou_env_file": f"CARIBOU environment file: {resolved.env_file}",
        "generated": f"new token saved in: {resolved.env_file}",
    }[resolved.source]
    console.print("[bold]Experiment access token[/bold]")
    console.print(resolved.value, markup=False)
    console.print(f"[dim]Source: {source}[/dim]")
    console.print(
        "[yellow]Anyone with this token can operate experiments on this CARIBOU server. "
        "Do not paste a model-provider API key here or share this output publicly.[/yellow]"
    )


@config_app.command("set-openai-key")
def set_api_key(
    api_key: str = typer.Argument(..., help="Your OpenAI API key (e.g., 'sk-...')"),
):
    """
    Saves your OpenAI API key to the CARIBOU environment file.
    """
    if not api_key.startswith("sk-"):
        console.print(
            "[yellow]Warning: Key does not look like a standard OpenAI API key (should start with 'sk-').[/yellow]"
        )

    # Ensure the .env file exists
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    os.chmod(ENV_FILE, 0o600)

    content = ENV_FILE.read_text()
    key_to_set = f'OPENAI_API_KEY="{api_key}"'

    # Use regex to safely replace the key if it already exists
    if re.search(r"^OPENAI_API_KEY=.*$", content, flags=re.MULTILINE):
        new_content = re.sub(
            r"^OPENAI_API_KEY=.*$", key_to_set, content, flags=re.MULTILINE
        )
    else:
        new_content = content + f"\n{key_to_set}\n"

    ENV_FILE.write_text(new_content.strip())
    os.chmod(ENV_FILE, 0o600)
    console.print(
        f"[bold green]✅ OpenAI API key has been set successfully in:[/bold green] {ENV_FILE}"
    )


@config_app.command("set-deepseek-key")
def set_deepseek_key(
    ctx: typer.Context,
    api_key: Optional[str] = typer.Argument(
        None, help="Your DeepSeek API key (e.g., 'ds_...')"
    ),
):
    """
    Saves your DeepSeek API key to the Caribou environment file.
    """
    if api_key is None:
        console.print("[bold red]Error:[/bold red] You must provide an API key.\n")
        if ctx.parent is not None:
            typer.echo(ctx.parent.get_help())
        raise typer.Exit()

    if not api_key.startswith("ds_"):
        console.print(
            "[yellow]Warning: Key does not look like a standard DeepSeek API key (should start with 'ds_').[/yellow]"
        )

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    os.chmod(ENV_FILE, 0o600)

    content = ENV_FILE.read_text()
    key_to_set = f'DEEPSEEK_API_KEY="{api_key}"'

    if re.search(r"^DEEPSEEK_API_KEY=.*$", content, flags=re.MULTILINE):
        new_content = re.sub(
            r"^DEEPSEEK_API_KEY=.*$", key_to_set, content, flags=re.MULTILINE
        )
    else:
        new_content = content.strip() + f"\n{key_to_set}\n"

    ENV_FILE.write_text(new_content)
    os.chmod(ENV_FILE, 0o600)
    console.print(
        f"[bold green]✅ DeepSeek API key has been set successfully in:[/bold green] {ENV_FILE}"
    )


@config_app.command("set-anthropic-key")
def set_anthropic_key(
    ctx: typer.Context,
    api_key: Optional[str] = typer.Argument(
        None, help="Your Anthropic API key (e.g., 'sk-ant-...')"
    ),
):
    """
    Saves your Anthropic API key to the Caribou environment file.
    """
    if api_key is None:
        console.print("[bold red]Error:[/bold red] You must provide an API key.\n")
        if ctx.parent is not None:
            typer.echo(ctx.parent.get_help())
        raise typer.Exit()

    if not api_key.startswith("sk-"):
        console.print(
            "[yellow]Warning: Key does not look like a standard Anthropic API key (should start with 'sk-').[/yellow]"
        )

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    os.chmod(ENV_FILE, 0o600)

    content = ENV_FILE.read_text()
    key_to_set = f'ANTHROPIC_API_KEY="{api_key}"'

    if re.search(r"^ANTHROPIC_API_KEY=.*$", content, flags=re.MULTILINE):
        new_content = re.sub(
            r"^ANTHROPIC_API_KEY=.*$", key_to_set, content, flags=re.MULTILINE
        )
    else:
        new_content = content.strip() + f"\n{key_to_set}\n"

    ENV_FILE.write_text(new_content.strip())
    os.chmod(ENV_FILE, 0o600)
    console.print(
        f"[bold green]✅ Anthropic API key has been set successfully in:[/bold green] {ENV_FILE}"
    )
