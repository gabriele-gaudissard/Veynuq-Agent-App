"""User-facing messages for rejected model responses."""


def model_response_message(lang, reason, tool=""):
    messages = {
        "en": (
            "The model sent an incomplete or invalid response. No action from that response ran.",
            "The response reached its output limit. No action from that response ran. You can continue in this chat and ask for smaller steps.",
            "The model sent an incomplete or invalid response. Retrying the current step.",
            "The model did not finish generating the file.",
            "Unfinished file drafts were discarded. Existing versions were preserved.",
        ),
        "it": (
            "Il modello ha inviato una risposta incompleta o non valida. Nessuna azione di quella risposta è stata eseguita.",
            "La risposta ha raggiunto il limite di output. Nessuna azione di quella risposta è stata eseguita. Puoi continuare nella stessa chat chiedendo passaggi più piccoli.",
            "Il modello ha inviato una risposta incompleta o non valida. Riprovo il passaggio corrente.",
            "Il modello non ha completato la generazione del file.",
            "Le bozze incomplete sono state scartate. Le versioni esistenti sono state conservate.",
        ),
        "es": (
            "El modelo envió una respuesta incompleta o no válida. No se ejecutó ninguna acción de esa respuesta.",
            "La respuesta alcanzó el límite de salida. No se ejecutó ninguna acción de esa respuesta. Puedes seguir en este chat y pedir pasos más pequeños.",
            "El modelo envió una respuesta incompleta o no válida. Reintentando el paso actual.",
            "El modelo no terminó de generar el archivo.",
            "Se descartaron los borradores incompletos. Se conservaron las versiones existentes.",
        ),
        "fr": (
            "Le modèle a envoyé une réponse incomplète ou invalide. Aucune action de cette réponse n’a été exécutée.",
            "La réponse a atteint sa limite de sortie. Aucune action de cette réponse n’a été exécutée. Vous pouvez continuer dans cette conversation et demander des étapes plus courtes.",
            "Le modèle a envoyé une réponse incomplète ou invalide. Nouvelle tentative pour l’étape en cours.",
            "Le modèle n’a pas terminé la génération du fichier.",
            "Les brouillons incomplets ont été supprimés. Les versions existantes ont été conservées.",
        ),
    }[lang if lang in ("it", "es", "fr") else "en"]
    index = {"length": 1, "retry": 2, "draft_incomplete": 3, "draft_discarded": 4}.get(reason, 0)
    text = messages[index]
    return f"{tool} — {text}" if tool else text
