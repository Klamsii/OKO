"""Local review page: source video, seekable event candidates and image evidence."""
import html
import json
from pathlib import Path
from .actions import RUSSIAN


def write_review(report):
    rows=[]
    for e in report['events']:
        title=RUSSIAN.get(e['type'],e['type'])
        bounds=e['bounds_seconds']
        interval=('начало видео' if bounds[0] is None else f'{bounds[0]:.3f}')+f' – {bounds[1]:.3f} с'
        pictures=''.join(f'<a href="{html.escape(Path(p).as_uri())}"><img src="{html.escape(Path(p).as_uri())}" alt="Кадр события"></a>' for p in e['evidence_frames'])
        description=html.escape(e.get('evidence',e.get('meaning','Нужна визуальная проверка последовательности.')))
        if e.get('object'):
            labels={'bag':'сумка','phone':'телефон','key':'ключ','card':'карта','cash':'деньги',
                    'paper_unknown':'неуточнённая бумага','other':'другой предмет','unknown':'неизвестный предмет'}
            description+='<p>Предмет: '+html.escape(labels.get(e['object'],e['object']))+'</p>'
        if e.get('from_locations') or e.get('to_locations'):
            description+='<p>Положение по ответу модели: '+html.escape(', '.join(e.get('from_locations',[])) or 'не наблюдалось')
            description+=' → '+html.escape(', '.join(e.get('to_locations',[])) or 'не наблюдается')+'</p>'
        if e.get('timing_status')=='model_estimated':
            description+='<p>Граница оценена моделью, визуально не подтверждена. Расстояние между исходными кадрами не означает точность времени события.</p>'
        rows.append(f'<tr><td><button data-time="{max(0,e["seconds"]-1):.3f}">{e["seconds"]:.3f} с</button></td>'
                    f'<td>{html.escape(title)}<p>{description}</p><small>{interval} · требует проверки</small></td><td>{pictures}</td></tr>')
    video=Path(report['source']).as_uri()
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Просмотр событий кассы</title>
<style>body{font:16px system-ui;margin:24px;background:#f5f7fa;color:#172638}h1{font-size:25px}
.layout{display:grid;grid-template-columns:330px 1fr;gap:24px}.player{position:sticky;top:20px;align-self:start}
video{width:100%;max-height:78vh;background:#111}table{border-collapse:collapse;width:100%;background:white}
td,th{border:1px solid #d8dfe7;padding:12px;text-align:left}td img{height:120px;max-width:100px;object-fit:contain}
button{border:0;border-radius:6px;padding:10px;background:#1255b5;color:white;cursor:pointer}p{margin:8px 0}small{color:#536274}
@media(max-width:800px){.layout{display:block}.player{position:static}video{height:350px}}</style>
<h1>Действия с деньгами и сейфом</h1><p>Это журнал наблюдений ИИ и кандидатов событий. Исчезновение денег из видимости не доказывает их перенос в карман.
Загрузка счётчика не доказывает запуск пересчёта. Тайм-код ведёт к исходному видео; интервал показывает границы между наблюдениями.</p>
<div class="layout"><div class="player"><video id="video" controls preload="metadata" src="VIDEO"></video>
<p>Если MOV не воспроизводится в браузере, откройте оригинал в локальном проигрывателе и используйте время в таблице.</p>
<a href="VIDEO">Оригинальное видео</a></div><table><thead><tr><th>Время</th><th>Событие</th><th>Кадры</th></tr></thead><tbody>ROWS</tbody></table></div>
<script>document.querySelectorAll('button[data-time]').forEach(b=>b.onclick=()=>{const v=document.getElementById('video');v.currentTime=Number(b.dataset.time);v.play().catch(()=>{});});</script></html>'''
    verdict=html.escape(report.get('Verdict','not_assessed'))
    status=html.escape(report.get('Status','unknown'))
    preliminary_note=('<p><strong>ПРЕДВАРИТЕЛЬНЫЙ ВЫВОД — BEST EFFORT.</strong> '+
        html.escape(report.get('decision_summary',''))+
        ' Найденные предметы используются как рабочие наблюдения. Сомнения сохранены, уверенность исходных ответов не изменена.</p>') if report.get('preliminary') else ''
    failed_reviews=[r for r in report.get('visual_episode_reviews',[]) if r.get('parse_error')]
    review_failures=('<h2>Дополнительные проверки без результата</h2><p>Эти уточнения не удалось подтвердить. '
        'Их отсутствие не доказывает, что действие не происходило.</p><ul>'+''.join(
        '<li>'+html.escape(r['question'])+f' · опорный кадр {r["anchor_seconds"]:.3f} с: '+
        html.escape(r['parse_error'])+'</li>' for r in failed_reviews)+'</ul>') if failed_reviews else ''
    findings=[]
    displayed=list(report.get('violations',[]))
    displayed.extend(v for v in report.get('possible_violations',[]) if not any(
        other.get('type')==v.get('type') and other.get('seconds')==v.get('seconds') for other in displayed))
    for v in displayed:
        basis=v.get('normative_basis') or {}
        source=(f'<a href="{html.escape(basis["url"])}">официальный текст</a>' if basis.get('url') else 'правовая норма не установлена')
        labels=(('Начало участка проверки','event_start'),('Опорный кадр','key_moment'),('Конец участка проверки','event_end')) if v.get('timeline_status')=='review_interval_not_confirmed_event' else (
            ('Начало','event_start'),('Ключевой момент','key_moment'),('Конец','event_end'))
        timeline=' · '.join(f'{label}: {html.escape(v.get(field) or "не установлено")}' for label,field in labels)
        evidence=''.join(f'<a href="{html.escape(Path(p).as_uri())}">кадр {i}</a> '
                         for i,p in enumerate(v.get('evidence_frames',[]),1))
        findings.append(f'<li><strong>{html.escape(v["type"])}</strong><p>{timeline}</p>'
                        f'<p>{html.escape(v["description"])}</p><p>Основание: '
                        f'{html.escape(basis.get("act") or "требуется правовая проверка")}; '
                        f'{html.escape(basis.get("provision") or "пункт не установлен")}. {source}. '
                        f'{html.escape(basis.get("note", ""))}</p><p>Кадры: {evidence}</p></li>')
    explanations={
        'unresolved_paper_identity':'Не установлено, являются ли видимые бумажные предметы банкнотами',
        'unresolved_cash_episode':'Не прослежены все необходимые звенья операции с деньгами',
        'unreadable_observations':'Не удалось разобрать отдельные кадры',
        'sequence_review_uncertain':'Хронологический анализ не подтвердил нарушение',
        'sequence_review_parse_error':'Ошибка ответа хронологического анализа',
        'unsupported_sequence_claim':'Предположение не подтверждено указанными кадрами',
        'partial_analysis':'Проверена только часть записи',
        'source_or_processing_incomplete':'Запись или обработка неполная',
        'no_observations':'Нет пригодных кадров'}
    reasons=''.join('<li>'+html.escape(explanations.get(r['code'],r['code']))+
        (': '+html.escape(', '.join(r.get('missing',[]))) if r.get('missing') else '')+'</li>'
        for r in report.get('abstention_reasons',[]))
    supplemental=report.get('supplemental_review_seconds')
    duration=supplemental if isinstance(supplemental,(int,float)) else report.get('test_wall_seconds',report.get('elapsed_seconds'))
    timing_label='Дополнительная проверка сохранённых наблюдений' if isinstance(supplemental,(int,float)) else 'Время прогона видео'
    elapsed=f' · <strong>{timing_label}:</strong> {duration:.1f} с' if isinstance(duration,(int,float)) else ''
    timing_details=' · '.join(f'{label}: {report[key]:.1f} с' for label,key in (
        ('Загрузка модели','model_load_seconds'),('Подготовка видео','video_prepare_seconds'),
        ('Анализ','inference_elapsed_seconds')) if isinstance(report.get(key),(int,float)) and
        (supplemental is None or key=='model_load_seconds'))
    source_duration=report.get('source_duration_seconds')
    analyzed=report.get('analyzed_duration_seconds')
    durations='<p>'+ ' · '.join(f'{label}: {value:.1f} с' for label,value in (
        ('Исходное видео',source_duration),('Просмотренный интервал',analyzed)) if isinstance(value,(int,float)))+'</p>'
    questions=[]
    for issue in report.get('review_issues',[]):
        a,b=issue.get('bounds_seconds') or [None,None]
        interval=f'{a:.3f}–{b:.3f} с' if isinstance(a,(int,float)) and isinstance(b,(int,float)) else 'интервал не установлен'
        impact=('Может изменить вердикт' if issue.get('affects_verdict') else 'Не меняет вердикт') if 'affects_verdict' in issue else (
            'Мешает выводу по эпизоду' if issue.get('blocking') else 'Не мешает проверке правила')
        questions.append('<tr><td>'+interval+'</td><td>'+html.escape(issue['description'])+'</td><td>'+
                         impact+'<p>'+html.escape(issue.get('impact_reason',''))+'</p></td></tr>')
    issue_table=('<h2>Открытые вопросы по эпизодам</h2><table><tr><th>Интервал</th><th>Что осталось выяснить</th><th>Значение</th></tr>'+
                 ''.join(questions)+'</table>') if questions else ''
    moments=report.get('moment_assessments',[])
    moment_table=('<h2>Вердикт по проверенным моментам</h2><p>Вывод предварительный и относится к просмотренным кадрам; промежутки между ними не считаются непрерывно проверенными.</p><table><tr><th>Интервал кадров</th><th>Вывод</th><th>Почему</th></tr>'+''.join(
        '<tr><td>'+f'{m["start_seconds"]:.3f}–{m["end_seconds"]:.3f} с'+'</td><td>'+html.escape(m['label'])+'</td><td>'+html.escape(m['reason'])+'</td></tr>' for m in moments)+'</table>') if moments else ''
    stages=report.get('stage_seconds') or {}
    if supplemental is not None and report.get('timing_scope')!='supplemental_review_not_full_video':
        stages={}  # Older supplemental exports inherited baseline stage timings.
    stage_details=('<details><summary>Время отдельных стадий</summary><ul>'+''.join(
        '<li>'+html.escape(k)+f': {v:.1f} с</li>' for k,v in stages.items())+'</ul></details>') if stages else ''
    assessment=(preliminary_note+f'<p><strong>Status:</strong> {status} · <strong>Verdict:</strong> {verdict}{elapsed}</p>'+
                (f'<p>{timing_details}</p>' if timing_details else '')+
                durations+
                (f'<p>Почему вывод не определён:</p><ul>{reasons}</ul>' if verdict=='insufficient_evidence' and reasons else '')+
                f'<h2>Предполагаемые нарушения</h2><ul>{"".join(findings) or "<li>Подтверждённых кандидатов нет</li>"}</ul>'+
                review_failures+moment_table+issue_table+stage_details)
    page=page.replace('<div class="layout">',assessment+'<div class="layout">')
    if report.get('fast_study'):
        page=page.replace('<div class="layout">','<p><strong>Выборочный ускоренный тест. До 20 обзорных и 12 уточняющих кадров. Это не полный анализ всех событий ролика.</strong></p><div class="layout">')
    if report.get('source_partial'):
        page=page.replace('<div class="layout">','<p><strong>Проверен только ограниченный фрагмент исходного видео. Дальнейшие действия не оценены.</strong></p><div class="layout">')
    target=Path(report['output'])/'review.html'
    target.write_text(page.replace('VIDEO',html.escape(video)).replace('ROWS',''.join(rows)),encoding='utf-8')
    return str(target)
