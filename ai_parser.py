import dateparser


def parse_tasks(text):

    # Split tasks using 'and'
    parts = text.split(" and ")

    tasks = []

    for part in parts:

        deadline = dateparser.parse(part)

        priority = "Low"

        if "urgent" in part.lower():
            priority = "High"

        elif "important" in part.lower():
            priority = "Medium"

        task_clean = part

        tasks.append({
            "task": task_clean,
            "priority": priority,
            "deadline": deadline
        })

    return tasks