import re


def classify(text):
    """Transparent V1 heuristics. No inferred identity or automatic direct outreach."""
    text = text.casefold()
    if re.search(r'\b(stop|unsubscribe)\b|не пишите|не интересует|отпис|不要联系|退订', text):
        return 0, 'D', 'opt-out', True
    rules = [
        (30, 'purchase', r'купить|куплю|покупк|заказ|buy|购买|订车'),
        (25, 'budget', r'бюджет|budget|预算|\d[\d\s.,]*\s*(₽|руб|usd|доллар|万元|万|rmb)'),
        (20, 'timing', r'сегодня|на этой неделе|в этом месяце|срочно|today|本周|本月|马上'),
        (15, 'logistics', r'достав|растамож|экспорт|delivery|运输|清关|出口'),
        (10, 'product', r'авто|машин|toyota|byd|geely|chery|tesla|bmw|mercedes|汽车|车型'),
    ]
    matched = [(points, reason) for points, reason, pattern in rules if re.search(pattern, text)]
    score = sum(x[0] for x in matched)
    grade = 'A+' if score >= 85 else 'A' if score >= 65 else 'B' if score >= 40 else 'C' if score >= 15 else 'D'
    return score, grade, ','.join(x[1] for x in matched) or 'no buying signal', False

