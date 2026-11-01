"""Conservative script-token ↔ Deepgram spans inside approved fragment ranges.

Exact anchors must occur in every optimal LCS, at the same rank. An unmatched
window is indivisible: its internal timing is unknown. Empty-sided windows absorb
both neighbouring anchors. A cue/shot can only cut at a proved paired boundary;
no proportional timing, guessed word IDs, or text substitutions are made.
"""
from production_state import ProductionError, digest


def unique_anchors(left, right):
    n,m=len(left),len(right)
    before=[[0]*(m+1) for _ in range(n+1)]
    after=[[0]*(m+1) for _ in range(n+1)]
    for i in range(n):
        for j in range(m):
            before[i+1][j+1]=before[i][j]+1 if left[i] and left[i]==right[j] else max(before[i][j+1],before[i+1][j])
    for i in range(n-1,-1,-1):
        for j in range(m-1,-1,-1):
            after[i][j]=after[i+1][j+1]+1 if left[i] and left[i]==right[j] else max(after[i+1][j],after[i][j+1])
    ranks={}
    for i in range(n):
        for j in range(m):
            if left[i] and left[i]==right[j] and before[i][j]+1+after[i+1][j+1]==before[n][m]:
                ranks.setdefault(before[i][j]+1,[]).append((i,j))
    return [pairs[0] for rank,pairs in sorted(ranks.items()) if len(pairs)==1]


def lexical_units(tokens):
    """Standalone written punctuation has no independent spoken word/time.

    Keep its exact token IDs with the preceding lexical unit (leading punctuation
    with the first one). This is orthographic grouping, not an ASR exception or a
    dropped word; numerals, negations and spoken symbols remain lexical units.
    """
    units=[];leading=[]
    silent=set('—–«»“”‘’„‟‚‛()[]{}.,:;!?…"'+"'")
    for token in tokens:
        if token['text'] and all(c in silent for c in token['text']):
            if units:units[-1]['last_token_id']=token['token_id']
            else:leading.append(token)
        else:
            units.append({**token,'token_id':leading[0]['token_id'] if leading else token['token_id'],
                'last_token_id':token['token_id']})
            leading=[]
    return units


def binding(script, words, fragments, aliases=None):
    from subtitles import canonical,script_tokens
    tokens=script_tokens(script);blocks=[]
    for index,parent in enumerate(fragments,1):
        local=lexical_units([t for t in tokens if t['fragment_index']==index])
        first,last=parent['first_word_id'],parent['last_word_id']
        source=words[first-1:last]
        if not local or not source:raise ProductionError('Missing verified fragment binding.')
        anchors=unique_anchors([canonical(t['text'],aliases) for t in local],
            [canonical(w.get('punctuated_word') or w['word'],aliases) for w in source])
        boundaries=[(0,0)]
        # An empty-sided gap is ambiguous, so neither adjacent anchor edge may
        # become a subtitle/shot boundary. Keep the surrounding window atomic.
        forbidden=set()
        pairs=[(-1,-1),*anchors,(len(local),len(source))]
        for (a,b),(c,d) in zip(pairs,pairs[1:]):
            if (c-a==1)!=(d-b==1):
                forbidden.update([(a,b),(a+1,b+1),(c,d),(c+1,d+1)])
        for i,j in anchors:
            for edge in [(i,j),(i+1,j+1)]:
                if edge not in forbidden and 0<edge[0]<len(local) and 0<edge[1]<len(source):boundaries.append(edge)
        boundaries=sorted(set([*boundaries,(len(local),len(source))]))
        for (a,b),(c,d) in zip(boundaries,boundaries[1:]):
            if not a<c or not b<d:raise ProductionError('Ambiguous non-monotonic anchor binding.')
            blocks.append({'fragment_index':index,'first_token_id':local[a]['token_id'],
                'last_token_id':local[c-1]['last_token_id'],'first_audio_word_id':first+b,'last_audio_word_id':first+d-1})
    proof={'policy':'unique_anchors_atomic_windows_v1','script_version_id':script.get('script_version_id'),
        'inputs_sha256':digest({'script':script,'words':words,'fragments':fragments,'aliases':aliases or {}}),'blocks':blocks}
    return proof


def verify_binding(proof,script,words,fragments,aliases=None):
    expected=binding(script,words,fragments,aliases)
    if proof!=expected:raise ProductionError('Stale/foreign script-word binding proof.')


def bound_range(proof,first,last):
    chosen=[b for b in proof['blocks'] if first<=b['first_token_id'] and b['last_token_id']<=last]
    if not chosen or chosen[0]['first_token_id']!=first or chosen[-1]['last_token_id']!=last:
        raise ProductionError('Ambiguous internal script/Deepgram boundary; regroup or repair only alignment.')
    return chosen[0]['first_audio_word_id'],chosen[-1]['last_audio_word_id']
