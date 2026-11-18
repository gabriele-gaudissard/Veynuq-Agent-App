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


def generation_message(lang, reason, partial=False):
    if reason in {"input_too_large", "compacting"}:
        messages = {
            "en": ("The current request and required instructions exceed the available context. Original messages were preserved. Split the input at meaningful sections or raise the configured context if supported.",
                   "Optimizing conversation memory to fit the available context…"),
            "it": ("La richiesta corrente e le istruzioni necessarie superano lo spazio disponibile. I messaggi originali sono conservati. Dividi il contenuto in sezioni coerenti o aumenta il contesto configurato se supportato.",
                   "Ottimizzo la memoria della conversazione per il contesto disponibile…"),
            "es": ("La solicitud actual y las instrucciones necesarias superan el contexto disponible. Los mensajes originales se conservaron. Divide la entrada en secciones coherentes o amplía el contexto si es compatible.",
                   "Optimizando la memoria de la conversación para el contexto disponible…"),
            "fr": ("La demande actuelle et les instructions nécessaires dépassent le contexte disponible. Les messages originaux sont conservés. Divisez le contenu en sections cohérentes ou augmentez le contexte si possible.",
                   "Optimisation de la mémoire de conversation pour le contexte disponible…"),
        }.get(lang)
        if messages is None:
            return generation_message("en", reason, partial)
        return messages[0 if reason == "input_too_large" else 1]
    localized = {
        "en": (
            "The response reached the output limit.",
            "There is too little room in the model context. Shorten the input or increase context if your hardware supports it.",
            "The model server ran out of memory. Try a smaller context or free memory.",
            "The model server timed out.",
            "The model returned too much data in a single response.",
            "Continuing the long response…",
            "The continuation repeated earlier text.",
            "The model did not return a safe text continuation.",
            "The model server interrupted the response.",
            "The partial text was saved. Send a follow-up to continue.",
        ),
        "it": (
            "La risposta ha raggiunto il limite di output.",
            "Lo spazio nel contesto del modello è insufficiente. Accorcia la richiesta o aumenta il contesto se l’hardware lo consente.",
            "Il server del modello ha esaurito la memoria. Riduci il contesto o libera memoria.",
            "Il server del modello non ha risposto entro il tempo previsto.",
            "Il modello ha inviato troppi dati in una singola risposta.",
            "Proseguo con la risposta lunga…",
            "La continuazione ha ripetuto il testo precedente.",
            "Il modello non ha restituito una continuazione testuale utilizzabile.",
            "Il server del modello ha interrotto la risposta.",
            "Il testo parziale è stato salvato. Invia un seguito per continuare.",
        ),
        "es": (
            "La respuesta alcanzó el límite de salida.",
            "Queda poco espacio en el contexto del modelo. Acorta la entrada o amplía el contexto si el equipo lo permite.",
            "El servidor del modelo se quedó sin memoria. Reduce el contexto o libera memoria.",
            "El servidor del modelo agotó el tiempo de espera.",
            "El modelo envió demasiados datos en una sola respuesta.",
            "Continuando la respuesta larga…",
            "La continuación repitió texto anterior.",
            "El modelo no devolvió una continuación de texto válida.",
            "El servidor del modelo interrumpió la respuesta.",
            "El texto parcial se guardó. Envía un mensaje para continuar.",
        ),
        "fr": (
            "La réponse a atteint la limite de sortie.",
            "Il reste trop peu de place dans le contexte du modèle. Raccourcissez la demande ou agrandissez le contexte si le matériel le permet.",
            "Le serveur du modèle a manqué de mémoire. Réduisez le contexte ou libérez de la mémoire.",
            "Le serveur du modèle a dépassé le délai d’attente.",
            "Le modèle a envoyé trop de données dans une seule réponse.",
            "Poursuite de la réponse longue…",
            "La suite a répété du texte déjà généré.",
            "Le modèle n’a pas fourni de suite textuelle exploitable.",
            "Le serveur du modèle a interrompu la réponse.",
            "Le texte partiel a été enregistré. Envoyez un nouveau message pour continuer.",
        ),
    }
    messages = localized.get(lang, localized["en"])
    index = {
        "length": 0, "context": 1, "memory": 2, "timeout": 3, "response_size": 4,
        "continuing": 5, "no_progress": 6, "structured": 7, "backend": 8,
    }.get(reason, 8)
    return messages[index] + (" " + messages[9] if partial else "")


def completion_message(lang, category):
    messages = {
        "en": {
            'edit_no_progress': 'The edit failed and the model kept reusing unchanged input or the same failed patch. Stopped without repeating the modification. Review TEXT_NOT_FOUND or AMBIGUOUS_MATCH in the tool result.',
            'edit_recovery_exhausted': 'The edit still failed after two different recovery attempts. No successful modification was reported; the original and any verified earlier work were preserved.',
            'checkpoint_no_progress': 'The model repeated checkpoints without new evidence. Stopped this loop; saved checkpoints do not prove that the requested work was performed.',
            'write_conflict_no_progress': 'The destination already exists. Recovery did not produce an available filename or an authorized replacement. The existing file was preserved; the failed write was not marked complete.',
            "repeated_analysis": 'The model repeated analysis or promises without producing the required action call. Recovery did not resolve it. Verified work and the original conversation were preserved.',
            "no_progress": 'The model kept repeating unchanged observations without making progress. Recovery was attempted once. Completed work and the original conversation were preserved; resume with a more specific next step.',
            "no_tool_call": "The model did not produce an action call needed to finish this request after the recovery attempts. The tool results show what was actually performed.",
            "empty_response": "The model returned an empty response after the recovery attempts. Review the saved operation results before continuing.",
            "partial": "Some operations failed. The operation results show the completed and failed steps. Inspect them before retrying.",
            "failed": "The requested operation failed. Check its result for the specific cause.",
            "unverified": "Actions were performed, but final verification was not completed. Inspect the pending targets before considering the task finished.",
            "limit": "The activity reached its iteration limit. Completed work was preserved. Resume to continue from the recorded results.",
        },
        "it": {
            'edit_no_progress': 'La modifica è fallita e il modello ripeteva letture invariate o la stessa sostituzione fallita. Ho fermato il ciclo senza ripetere la modifica. Controlla TEXT_NOT_FOUND o AMBIGUOUS_MATCH nel risultato.',
            'edit_recovery_exhausted': 'La modifica è fallita anche dopo due tentativi di recupero diversi. Non è stata dichiarata completata; l’originale e gli eventuali risultati precedenti verificati sono conservati.',
            'checkpoint_no_progress': 'Il modello ripeteva checkpoint senza nuove evidenze. Ho fermato il ciclo: salvare un checkpoint non dimostra che il lavoro richiesto sia stato eseguito.',
            'write_conflict_no_progress': 'Il file di destinazione esiste già. Il recupero non ha prodotto un nome disponibile o una sostituzione autorizzata. Il file esistente è conservato e la scrittura fallita non è dichiarata completata.',
            "repeated_analysis": 'Il modello ha ripetuto analisi o intenzioni senza produrre la chiamata operativa necessaria. Il tentativo di recupero non è riuscito. Il lavoro verificato e la conversazione originale sono conservati.',
            "no_progress": 'Il modello continuava a ripetere le stesse letture senza avanzare. È stato tentato un recupero. Il lavoro completato e la conversazione originale sono conservati; puoi riprendere indicando il prossimo passo.',
            "no_tool_call": "Il modello non ha prodotto la chiamata operativa necessaria per completare questa richiesta dopo i tentativi di recupero. I risultati degli strumenti mostrano ciò che è stato effettivamente eseguito.",
            "empty_response": "Il modello ha restituito una risposta vuota dopo i tentativi di recupero. Controlla i risultati salvati delle operazioni prima di continuare.",
            "partial": "Alcune operazioni sono fallite. Nei risultati trovi i passaggi completati e quelli falliti. Controllali prima di riprovare.",
            "failed": "L’operazione richiesta è fallita. Il risultato indica la causa specifica.",
            "unverified": "Sono state eseguite azioni, ma la verifica finale non è stata completata. Controlla gli elementi in attesa prima di considerare concluso il lavoro.",
            "limit": "L’attività ha raggiunto il limite di iterazioni. Il lavoro completato è conservato. Riprendi per proseguire dai risultati registrati.",
        },
        "es": {
            'edit_no_progress': 'La modificación falló y el modelo repitió lecturas sin cambios o el mismo parche fallido. Se detuvo el ciclo. Revisa TEXT_NOT_FOUND o AMBIGUOUS_MATCH en el resultado.',
            'edit_recovery_exhausted': 'La modificación falló tras dos intentos de recuperación diferentes. Se conservaron el original y el trabajo verificado; no se declaró completada.',
            'checkpoint_no_progress': 'El modelo repitió puntos de control sin nuevas pruebas. Se detuvo el ciclo; guardarlos no demuestra que el trabajo se haya realizado.',
            'write_conflict_no_progress': 'El destino ya existe. La recuperación no produjo un nombre disponible ni una sustitución autorizada. Se conservó el archivo existente; la escritura fallida no se declaró completada.',
            "repeated_analysis": 'El modelo repitió análisis o intenciones sin producir la llamada necesaria. La recuperación no funcionó. Se conservaron el trabajo verificado y la conversación original.',
            "no_progress": 'El modelo repitió observaciones sin cambios y sin avanzar. Se intentó una recuperación. El trabajo y la conversación se conservaron; reanuda indicando el siguiente paso.',
            "no_tool_call": "El modelo no generó una llamada de acción para esta solicitud tras los intentos de recuperación. Los resultados muestran las operaciones realizadas.",
            "empty_response": "El modelo devolvió una respuesta vacía tras los intentos de recuperación. Revisa los resultados guardados antes de continuar.",
            "partial": "Algunas operaciones fallaron. Los resultados indican los pasos completados y fallidos. Revísalos antes de reintentar.",
            "failed": "La operación solicitada falló. El resultado indica la causa específica.",
            "unverified": "Se realizaron acciones, pero la verificación final está pendiente. Revisa los objetivos pendientes antes de dar la tarea por terminada.",
            "limit": "La actividad alcanzó el límite de iteraciones. El trabajo completado se conservó. Reanuda desde los resultados registrados.",
        },
        "fr": {
            'edit_no_progress': 'La modification a échoué et le modèle répétait les mêmes lectures ou le même remplacement. La boucle a été arrêtée. Consultez TEXT_NOT_FOUND ou AMBIGUOUS_MATCH dans le résultat.',
            'edit_recovery_exhausted': 'La modification a échoué après deux tentatives de reprise différentes. L’original et le travail vérifié sont conservés ; elle n’a pas été déclarée terminée.',
            'checkpoint_no_progress': 'Le modèle répétait des points de reprise sans nouvelle preuve. La boucle a été arrêtée ; leur sauvegarde ne prouve pas que le travail a été effectué.',
            'write_conflict_no_progress': 'La destination existe déjà. La reprise n’a produit ni nom disponible ni remplacement autorisé. Le fichier existant est conservé ; l’écriture échouée n’a pas été déclarée terminée.',
            "repeated_analysis": 'Le modèle a répété des analyses ou intentions sans produire l’appel nécessaire. La reprise a échoué. Le travail vérifié et la conversation originale sont conservés.',
            "no_progress": 'Le modèle répétait les mêmes observations sans avancer. Une reprise a été tentée. Le travail et la conversation sont conservés ; reprenez en précisant la prochaine étape.',
            "no_tool_call": "Le modèle n’a pas produit d’appel d’action après les tentatives de récupération. Les résultats indiquent les opérations réellement exécutées.",
            "empty_response": "Le modèle a renvoyé une réponse vide après les tentatives de récupération. Vérifiez les résultats enregistrés avant de continuer.",
            "partial": "Certaines opérations ont échoué. Les résultats indiquent les étapes terminées et celles en échec. Vérifiez-les avant de réessayer.",
            "failed": "L’opération demandée a échoué. Le résultat précise la cause.",
            "unverified": "Des actions ont été effectuées, mais leur vérification finale reste en attente. Vérifiez les cibles concernées avant de terminer.",
            "limit": "L’activité a atteint la limite d’itérations. Le travail effectué est conservé. Reprenez depuis les résultats enregistrés.",
        },
    }
    return messages.get(lang, messages["en"]).get(category, messages["en"]["failed"])
