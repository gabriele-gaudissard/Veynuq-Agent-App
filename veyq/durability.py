"""Bounded, local task state and context checkpoints; never replay uncertain writes."""
import copy
import json
import time


def compact(session, limit):
    history = session['history']
    eligible = [i for i, m in enumerate(history) if m.get('role') == 'user']
    cut = 0
    while len(json.dumps(history[cut:])) > limit and len(eligible) > 2:
        eligible.pop(0)
        cut = eligible[0]
    if cut:
        notes = [session.get('context_summary', '')]
        previous = min(session.get('context_offset', 0), cut)
        for message in history[previous:cut]:
            role = message.get('role')
            content = str(message.get('content', ''))
            if role == 'user':
                notes.append('User request: ' + content[:1200])
            elif role == 'tool':
                notes.append('Observed tool ' + message.get('name', '') + ': ' + content[:800])
            elif role == 'assistant' and content:
                notes.append('Earlier assistant statement (verify before relying on it): ' + content[:400])
        session['context_summary'] = '\n'.join(notes)[-10000:]
        session['context_offset'] = cut
    result = copy.deepcopy(history[cut:])
    for message in result:
        if message.get('role') == 'tool' and len(message.get('content', '')) > 5000:
            message['content'] = message['content'][:5000] + '\n[Earlier output shortened; inspect the source again when needed.]'
    # A legacy failed response could have been persisted before validation.
    # Keep the transcript on disk, and send the provider only usable exchanges.
    from .engine import decode_arguments
    index = 0
    while index < len(result):
        calls = result[index].get('tool_calls') or []
        if not calls:
            index += 1
            continue
        ids, end = tool_exchange(result, index)
        valid = len(ids) == len(calls) and None not in ids and {m.get('tool_call_id') for m in result[index+1:end]} == ids
        if valid:
            try:
                valid = all(isinstance(decode_arguments(c['function'].get('arguments', {})), dict) for c in calls)
            except (KeyError, TypeError, ValueError, RecursionError):
                valid = False
        write = any(c.get('function', {}).get('name') == 'write_file' for c in calls)
        if not valid or (write and len(json.dumps(result)) > limit):
            result[index:end] = [tool_observation(result[index+1:end])]
            index += 1
        else:
            index = end
    # A long single task can contain many tool exchanges without a new user turn.
    # Replace whole completed exchanges; preserve requests and the recent tail.
    while len(json.dumps(result)) > limit:
        index = next((i for i, m in enumerate(result[:-4]) if m.get('role') == 'assistant' and m.get('tool_calls')), None)
        if index is None:
            index = next((i for i, m in enumerate(result[:-4]) if m.get('role') == 'assistant' and not m.get('tool_calls')), None)
            if index is None: break
            result.pop(index)
            continue
        ids, end = tool_exchange(result, index)
        if {m.get('tool_call_id') for m in result[index+1:end]} != ids: break
        result[index:end] = [tool_observation(result[index+1:end])]
    return result


def tool_exchange(messages, index):
    ids = {c.get('id') for c in messages[index]['tool_calls']}
    end = index + 1
    while end < len(messages) and messages[end].get('role') == 'tool' and messages[end].get('tool_call_id') in ids:
        end += 1
    return ids, end


def tool_observation(results):
    if not results:
        return {'role': 'assistant', 'content': 'Earlier tool response has no recorded outcome. Inspect actual state before retrying.'}
    note = '\n'.join(m.get('name', '') + ': ' + str(m.get('content', ''))[:350] for m in results)
    return {'role': 'assistant', 'content': 'Earlier tool observations (verify before reuse): ' + note[:1500]}


def checkpoint(session, state, **values):
    session['task'] = {**session.get('task', {}), **values, 'state': state, 'updated_at': time.time()}


def recover(session):
    for proposal in session.get('proposals', []):
        if proposal.get('state') == 'running': proposal['state'] = 'interrupted'
    task = session.get('task', {})
    if task.get('state') in {'running', 'approval', 'question'}:
        checkpoint(session, 'interrupted')
    # A crash after dispatch may leave assistant tool calls without results.
    # Preserve the pair and explicitly mark its outcome unknown, never replay it.
    history = session.get('history', [])
    answered = {m.get('tool_call_id') for m in history if m.get('role') == 'tool'}
    repaired = []
    for message in history:
        repaired.append(message)
        for call in message.get('tool_calls', []):
            if call.get('id') not in answered:
                repaired.append({'role': 'tool', 'tool_call_id': call['id'], 'name': call.get('function', {}).get('name', ''),
                                 'content': json.dumps({'ok': False, 'outcome_unknown': True,
                                     'error': 'Application stopped before recording this result. Inspect the actual state before any retry; the action may already have happened.'})})
                answered.add(call['id'])
    session['history'] = repaired
