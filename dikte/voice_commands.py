"""Voice command processing for hands-free editing during dictation.

Processes commands like "new paragraph", "delete that", "capitalize" before
cleanup, allowing users to edit without breaking flow.

Commands are detected in the raw transcript and applied immediately. Custom
snippets and user-defined commands are supported through config.
"""

import re
from .i18n import t


def _new_paragraph(text, match):
    """Insert paragraph break (double newline)."""
    before = text[:match.start()].rstrip(' ')
    after = text[match.end():].lstrip(' ')
    return before + "\n\n" + after

def _delete_last(text, match):
    """Delete the previous sentence or phrase."""
    before = text[:match.start()].rstrip(' ')
    # Find last sentence boundary
    sentences = re.split(r'[.!?]\s+', before)
    if len(sentences) > 1:
        before = '. '.join(sentences[:-1]) + '.'
    else:
        before = ""
    after = text[match.end():].lstrip(' ')
    return (before + " " + after).strip(' ')

def _insert_period(text, match):
    """Insert period."""
    before = text[:match.start()].rstrip(' ')
    after = text[match.end():].lstrip(' ')
    return before + ". " + after.capitalize()

def _insert_comma(text, match):
    """Insert comma."""
    before = text[:match.start()].rstrip(' ')
    after = text[match.end():].lstrip(' ')
    return before + ", " + after

def _capitalize_next(text, match):
    """Capitalize the next word."""
    before = text[:match.start()]
    after = text[match.end():].lstrip(' ')
    if after:
        after = after[0].upper() + after[1:]
    return (before + " " + after).strip(' ')

def _new_line(text, match):
    """Insert single line break."""
    before = text[:match.start()].rstrip(' ')
    after = text[match.end():].lstrip(' ')
    return before + "\n" + after

# Built-in commands: pattern → (handler, description)
# Case-insensitive matching
BUILTIN_COMMANDS = {
    # Paragraph control
    r'\b(new paragraph|yeni paragraf)\b': (_new_paragraph, "Insert paragraph break"),
    r'\b(new line|yeni satır)\b': (_new_line, "Insert line break"),
    
    # Editing
    r'\b(delete that|scratch that|sil|sil onu)\b': (_delete_last, "Delete previous sentence"),
    
    # Punctuation
    r'\b(period|nokta)\b': (_insert_period, "Insert period"),
    r'\b(comma|virgül)\b': (_insert_comma, "Insert comma"),
    
    r'\b(capitalize|cap|büyük harf)\b': (_capitalize_next, "Capitalize next word"),
}


def process_commands(text, config):
    """Process voice commands in transcript text.
    
    Args:
        text: Raw transcript from speech-to-text
        config: Configuration dict with voice_commands settings
        
    Returns:
        Processed text with commands applied and removed
    """
    if not config.get("voice_commands_enabled", False):
        return text
    
    # Get custom snippets from config
    snippets = config.get("voice_snippets", {})
    
    # Apply custom snippets first (simple string replacement)
    for trigger, replacement in snippets.items():
        # Case-insensitive replacement
        pattern = re.compile(re.escape(trigger), re.IGNORECASE)
        text = pattern.sub(replacement, text)
    
    # Apply built-in commands
    # Re-scan and apply one command at a time to handle position changes
    changed = True
    max_iterations = 20  # Prevent infinite loops
    iterations = 0
    
    while changed and iterations < max_iterations:
        changed = False
        iterations += 1
        
        for pattern_str, (handler, desc) in BUILTIN_COMMANDS.items():
            pattern = re.compile(pattern_str, re.IGNORECASE)
            match = pattern.search(text)
            if match:
                text = handler(text, match)
                changed = True
                break  # Re-scan from start with updated text
    return text.strip(' ')


def available_commands():
    """Return list of available commands for UI/help.

    Returns:
        List of (trigger, description) tuples
    """
    commands = []
    for pattern, (_, desc) in BUILTIN_COMMANDS.items():
        # Extract readable trigger from regex pattern
        trigger = pattern.replace(r'\b', '').replace('(', '').replace(')', '')
        trigger = trigger.split('|')[0]  # Take first variant
        commands.append((trigger, desc))
    return commands


def snippets_to_text(snippets):
    """Render the {trigger: replacement} config value as editable lines.

    One "trigger: replacement" per line, which is what the settings window
    shows and what snippets_from_text() reads back.
    """
    return "\n".join(f"{trigger}: {replacement}"
                      for trigger, replacement in snippets.items())


def snippets_from_text(text):
    """Parse the settings window's textbox back into {trigger: replacement}.

    A line with no colon, or an empty trigger, is dropped rather than raising:
    it is what a half-typed line looks like while the user is still editing.
    """
    snippets = {}
    for line in text.splitlines():
        trigger, sep, replacement = line.partition(":")
        trigger = trigger.strip()
        if sep and trigger:
            snippets[trigger] = replacement.strip()
    return snippets
