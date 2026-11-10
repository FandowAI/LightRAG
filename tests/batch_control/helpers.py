from lightrag.batch_control.contracts import Configuration


def change(document, section, **updates):
    data = document.model_dump()
    data[section].update(updates)
    return Configuration.model_validate(data)
