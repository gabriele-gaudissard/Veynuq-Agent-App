"""Choose relevant existing tools without changing their permission gates."""
import re

READ = {"list_dir", "read_file", "search_files", "read_document", "view_image",
        "inspect_binary", "code_intelligence", "git_status", "read_project_context"}
FILES = {"list_dir", "read_file", "search_files", "write_file", "edit_file", "make_dir", "move_file", "delete_file", "restore_backup"}
CONTROL = {"ask_user", "update_plan", "checkpoint_task", "load_procedure"}
NETWORK = {"web_search", "read_url", "download_file", "browser_open", "browser_state", "browser_action"}
ACQUIRE = {"tool_setup", "plugin_catalog", "plugin_marketplace", "plugin_tools", "plugin_call", "plugin_extension"}
DESKTOP = {"computer_windows", "computer_inspect", "computer_action", "computer_pointer",
           "computer_wait", "computer_sandbox_type", "windows_sandbox", "keyboard_layout"}
CODE = FILES | READ | {"exec_cmd", "clone_repository", "git_worktree", "sandbox_changes",
                "delegate_tasks", "delegate_coding", "proposal_changes"} | ACQUIRE
RESOURCE = re.compile(r"(?i)(?:[a-z]:[\\/]|https?://|\b[\w.-]+\.(?:py|js|ts|txt|md|json|exe|pdf|docx|ino|cpp|html)\b)")
DISPLAY = re.compile(r"(?i)\b(?:show|display|paste|print|visualizza|mostra|incolla|stampa)\w*\b.{0,60}\b(?:here|chat|qui|screen|schermo)\b|"
                     r"\b(?:only|solo|soltanto)\s+(?:text|testo|codice|code|explanation|spiegazione|istruzioni)\b")
PROSE = re.compile(r"(?i)\b(?:poem|poesia|story|racconto|letter|lettera|paragrafo|paragraph|email|joke|barzelletta)\b")
CODE_TEXT = re.compile(r"(?i)\b(?:write|scrivi|scrivimi|genera|generate)\b.{0,45}\b(?:codice|code|function|funzione|snippet|script)\b")
DISK = re.compile(r"(?i)\b(?:save|salva|salvalo|salvami|folder|cartella|directory|repository|repo|project|progetto|app|application|applicazione|"
                  r"disk|disco|execute|esegui|run)\b|\b(?:create|crea)\b.{0,30}\b(?:file|cartella|folder)\b")


def text_only_request(text):
    text = text.strip()
    if re.search(r"(?i)\b(?:then|poi|e dopo|and|e)\s+(?:save|salva\w*|execute|esegui\w*|run|crea|create|modifica|modify)\b", text):
        return False
    if re.match(r"(?i)^(?:how (?:do|can|to)|come (?:posso|si|faccio)|explain how|spiega\w* come)\b", text):
        return True
    if re.match(r"(?i)^(?:explain|spiega\w*)\b", text) and not RESOURCE.search(text) and not re.search(r"(?i)\b(?:file|folder|cartella|project|progetto|repository|repo)\b", text):
        return True
    if DISPLAY.search(text):
        return not RESOURCE.search(text) and not re.search(r"(?i)\b(?:also save|salva anche|e salva|and save|esegui|execute)\b", text)
    if PROSE.search(text) and not RESOURCE.search(text) and not re.search(r"(?i)\b(?:salva|save|file|invia|send)\b", text):
        return True
    return bool(CODE_TEXT.search(text) and not RESOURCE.search(text) and not DISK.search(text))


def select_tools(request, available, required=False, previous=(), *, operational=False):
    """Return a subset of the already-authorized pool, in its original order."""
    if text_only_request(request):
        return [], "none"
    text = request.lower()
    chosen = set()
    if RESOURCE.search(text) or re.search(r"\b(file|cartell\w*|folder|directory|progett\w*|project|codice|code|script)\b", text):
        chosen |= FILES if required or operational else READ
    if re.search(r"\b(codebase|repository|repo|github|configur\w*|install\w*|compil\w*|implement\w*|build|debug|test\w*|bug|app|application|applicazione)\b", text):
        chosen |= CODE
    if re.search(r"\b(github|repository|repo)\b", text):
        chosen |= {"github", "clone_repository"} | NETWORK
    if re.search(r"\b(web|online|internet|browser|download|scarica\w*|ricerca|research|latest|recent\w*|meteo|weather)\b|https?://", text):
        chosen |= NETWORK
    if re.search(r"\b(pc|desktop|mouse|keyboard|tastier\w*|windows|schermo|screen|finestr\w*|window|impostazion\w*)\b", text):
        chosen |= DESKTOP | FILES
    if re.search(r"\b(immagin\w*|image\w*|picture|foto|logo|disegn\w*)\b", text):
        chosen |= {"generate_image", "view_image", "list_dir"}
    if re.search(r"\b(plugin\w*|packages?|pacchett\w*|pypi|pip)\b|\b(?:install\w*|scarica\w*|download|cerca|find)\b.{0,45}\b(?:strument\w*|tools?)\b", text):
        chosen |= ACQUIRE | NETWORK | {"exec_cmd", "list_dir", "read_file"}
    if re.search(r"\b(binary|binario|exe|dll|decompil\w*|disassembl\w*)\b", text):
        chosen |= FILES | ACQUIRE | NETWORK | {"exec_cmd"}
    if re.search(r"\b(memori\w*|remember|ricorda\w*|preferenc\w*)\b", text):
        chosen |= {"save_memory", "read_project_context"}
    if (required or operational) and not chosen:
        return list(available), "required" if required else "auto"
    if not chosen:
        return [], "none"
    # Continue using the tools involved in earlier stages of this same task.
    chosen.update(previous)
    chosen |= CONTROL
    selected = [tool for tool in available if tool["function"]["name"] in chosen]
    return selected, "required" if required and selected else "auto" if selected else "none"


def continued_scope(request, history):
    """A short resume keeps the previous task's tool domains, in auto mode."""
    continuation = r"(?i)(?:continue|proceed|go on|resume|continua|prosegui|riprendi|vai avanti)[.! ]*"
    if not re.fullmatch(continuation, request.strip()):
        return request
    for message in reversed(history):
        prior = message.get("content", "")
        if message.get("role") == "user" and not re.fullmatch(continuation, prior.strip()):
            return prior + "\nFollow-up: " + request
    return request
