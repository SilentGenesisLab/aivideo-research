"""JSON Schema helpers for the temporal model contract."""

def obj(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def arr(items):
    return dict(type='array', items=items)


TEXT = {'type': 'string'}
NUMBER = {'type': 'number'}
