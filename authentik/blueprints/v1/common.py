"""transfer common classes"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from functools import reduce
from operator import ixor
from os import getenv
from typing import Any, Iterable, Literal, Optional, Union
from uuid import UUID

from django.apps import apps
from django.db.models import Model, Q
from rest_framework.fields import Field
from rest_framework.serializers import Serializer
from yaml import SafeDumper, SafeLoader, ScalarNode, SequenceNode

from authentik.lib.models import SerializerModel
from authentik.lib.sentry import SentryIgnoredException
from authentik.policies.models import PolicyBindingModel


def get_attrs(obj: SerializerModel) -> dict[str, Any]:
    """Get object's attributes via their serializer, and convert it to a normal dict"""
    serializer: Serializer = obj.serializer(obj)
    data = dict(serializer.data)

    for field_name, _field in serializer.fields.items():
        _field: Field
        if field_name not in data:
            continue
        if _field.read_only:
            data.pop(field_name, None)
        if _field.get_initial() == data.get(field_name, None):
            data.pop(field_name, None)
        if field_name.endswith("_set"):
            data.pop(field_name, None)
    return data


@dataclass
class BlueprintEntryState:
    """State of a single instance"""

    instance: Optional[Model] = None


class BlueprintEntryDesiredState(Enum):
    """State an entry should be reconciled to"""

    ABSENT = "absent"
    PRESENT = "present"
    CREATED = "created"


@dataclass
class BlueprintEntry:
    """Single entry of a blueprint"""

    model: Union[str, "YAMLTag"]
    state: Union[BlueprintEntryDesiredState, "YAMLTag"] = field(
        default=BlueprintEntryDesiredState.PRESENT
    )
    conditions: list[Any] = field(default_factory=list)
    identifiers: dict[str, Any] = field(default_factory=dict)
    attrs: Optional[dict[str, Any]] = field(default_factory=dict)

    # pylint: disable=invalid-name
    id: Optional[str] = None

    _state: BlueprintEntryState = field(default_factory=BlueprintEntryState)

    def __post_init__(self, *args, **kwargs) -> None:
        self.__tag_contexts: list["YAMLTagContext"] = []

    @staticmethod
    def from_model(model: SerializerModel, *extra_identifier_names: str) -> "BlueprintEntry":
        """Convert a SerializerModel instance to a blueprint Entry"""
        identifiers = {
            "pk": model.pk,
        }
        all_attrs = get_attrs(model)

        for extra_identifier_name in extra_identifier_names:
            identifiers[extra_identifier_name] = all_attrs.pop(extra_identifier_name, None)
        return BlueprintEntry(
            identifiers=identifiers,
            model=f"{model._meta.app_label}.{model._meta.model_name}",
            attrs=all_attrs,
        )

    def _get_tag_context(
        self,
        depth: int = 0,
        context_tag_type: Optional[type["YAMLTagContext"] | tuple["YAMLTagContext", ...]] = None,
    ) -> "YAMLTagContext":
        """Get a YAMLTagContex object located at a certain depth in the tag tree"""
        if depth < 0:
            raise ValueError("depth must be a positive number or zero")

        if context_tag_type:
            contexts = [x for x in self.__tag_contexts if isinstance(x, context_tag_type)]
        else:
            contexts = self.__tag_contexts

        try:
            return contexts[-(depth + 1)]
        except IndexError:
            raise ValueError(f"invalid depth: {depth}. Max depth: {len(contexts) - 1}")

    def tag_resolver(self, value: Any, blueprint: "Blueprint") -> Any:
        """Check if we have any special tags that need handling"""
        val = deepcopy(value)

        if isinstance(value, YAMLTagContext):
            # Only add the tag context if it has not already been added.
            # This can happen in some edge cases where a sequence sits between
            # nested context tags
            if not self.__tag_contexts or self.__tag_contexts[-1] is not value:
                self.__tag_contexts.append(value)

        if isinstance(value, YAMLTag):
            val = value.resolve(self, blueprint)

        if isinstance(value, dict):
            for key, inner_value in value.items():
                val[key] = self.tag_resolver(inner_value, blueprint)
        if isinstance(value, list):
            for idx, inner_value in enumerate(value):
                val[idx] = self.tag_resolver(inner_value, blueprint)

        if isinstance(value, YAMLTagContext):
            self.__tag_contexts.pop()

        return val

    def get_attrs(self, blueprint: "Blueprint") -> dict[str, Any]:
        """Get attributes of this entry, with all yaml tags resolved"""
        return self.tag_resolver(self.attrs, blueprint)

    def get_identifiers(self, blueprint: "Blueprint") -> dict[str, Any]:
        """Get attributes of this entry, with all yaml tags resolved"""
        return self.tag_resolver(self.identifiers, blueprint)

    def get_state(self, blueprint: "Blueprint") -> BlueprintEntryDesiredState:
        """Get the blueprint state, with yaml tags resolved if present"""
        return BlueprintEntryDesiredState(self.tag_resolver(self.state, blueprint))

    def get_model(self, blueprint: "Blueprint") -> str:
        """Get the blueprint model, with yaml tags resolved if present"""
        return str(self.tag_resolver(self.model, blueprint))

    def check_all_conditions_match(self, blueprint: "Blueprint") -> bool:
        """Check all conditions of this entry match (evaluate to True)"""
        return all(self.tag_resolver(self.conditions, blueprint))


@dataclass
class BlueprintMetadata:
    """Optional blueprint metadata"""

    name: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Blueprint:
    """Dataclass used for a full export"""

    version: int = field(default=1)
    entries: list[BlueprintEntry] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    metadata: Optional[BlueprintMetadata] = field(default=None)


class YAMLTag:
    """Base class for all YAML Tags"""

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        """Implement yaml tag logic"""
        raise NotImplementedError


class YAMLTagContext:
    """Base class for all YAML Tag Contexts"""

    def get_context(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        """Implement yaml tag context logic"""
        raise NotImplementedError


class KeyOf(YAMLTag):
    """Reference another object by their ID"""

    id_from: str

    # pylint: disable=unused-argument
    def __init__(self, loader: "BlueprintLoader", node: ScalarNode) -> None:
        super().__init__()
        self.id_from = node.value

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        for _entry in blueprint.entries:
            if _entry.id == self.id_from and _entry._state.instance:
                # Special handling for PolicyBindingModels, as they'll have a different PK
                # which is used when creating policy bindings
                if (
                    isinstance(_entry._state.instance, PolicyBindingModel)
                    and entry.model.lower() == "authentik_policies.policybinding"
                ):
                    return _entry._state.instance.pbm_uuid
                return _entry._state.instance.pk
        raise EntryInvalidError(
            f"KeyOf: failed to find entry with `id` of `{self.id_from}` and a model instance"
        )


class Env(YAMLTag):
    """Lookup environment variable with optional default"""

    key: str
    default: Optional[Any]

    # pylint: disable=unused-argument
    def __init__(self, loader: "BlueprintLoader", node: ScalarNode | SequenceNode) -> None:
        super().__init__()
        self.default = None
        if isinstance(node, ScalarNode):
            self.key = node.value
        if isinstance(node, SequenceNode):
            self.key = node.value[0].value
            self.default = node.value[1].value

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        return getenv(self.key, self.default)


class Context(YAMLTag):
    """Lookup key from instance context"""

    key: str
    default: Optional[Any]

    # pylint: disable=unused-argument
    def __init__(self, loader: "BlueprintLoader", node: ScalarNode | SequenceNode) -> None:
        super().__init__()
        self.default = None
        if isinstance(node, ScalarNode):
            self.key = node.value
        if isinstance(node, SequenceNode):
            self.key = node.value[0].value
            self.default = node.value[1].value

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        value = self.default
        if self.key in blueprint.context:
            value = blueprint.context[self.key]
        return value


class Format(YAMLTag):
    """Format a string"""

    format_string: str
    args: list[Any]

    # pylint: disable=unused-argument
    def __init__(self, loader: "BlueprintLoader", node: SequenceNode) -> None:
        super().__init__()
        self.format_string = node.value[0].value
        self.args = []
        for raw_node in node.value[1:]:
            self.args.append(loader.construct_object(raw_node))

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        args = []
        for arg in self.args:
            if isinstance(arg, YAMLTag):
                args.append(arg.resolve(entry, blueprint))
            else:
                args.append(arg)

        try:
            return self.format_string % tuple(args)
        except TypeError as exc:
            raise EntryInvalidError(exc)


class Find(YAMLTag):
    """Find any object"""

    model_name: str
    conditions: list[list]

    model_class: type[Model]

    def __init__(self, loader: "BlueprintLoader", node: SequenceNode) -> None:
        super().__init__()
        self.model_name = node.value[0].value
        self.model_class = apps.get_model(*self.model_name.split("."))
        self.conditions = []
        for raw_node in node.value[1:]:
            values = []
            for node_values in raw_node.value:
                values.append(loader.construct_object(node_values))
            self.conditions.append(values)

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        query = Q()
        for cond in self.conditions:
            if isinstance(cond[0], YAMLTag):
                query_key = cond[0].resolve(entry, blueprint)
            else:
                query_key = cond[0]
            if isinstance(cond[1], YAMLTag):
                query_value = cond[1].resolve(entry, blueprint)
            else:
                query_value = cond[1]
            query &= Q(**{query_key: query_value})
        instance = self.model_class.objects.filter(query).first()
        if instance:
            return instance.pk
        return None


class Condition(YAMLTag):
    """Convert all values to a single boolean"""

    mode: Literal["AND", "NAND", "OR", "NOR", "XOR", "XNOR"]
    args: list[Any]

    _COMPARATORS = {
        # Using all and any here instead of from operator import iand, ior
        # to improve performance
        "AND": all,
        "NAND": lambda args: not all(args),
        "OR": any,
        "NOR": lambda args: not any(args),
        "XOR": lambda args: reduce(ixor, args) if len(args) > 1 else args[0],
        "XNOR": lambda args: not (reduce(ixor, args) if len(args) > 1 else args[0]),
    }

    # pylint: disable=unused-argument
    def __init__(self, loader: "BlueprintLoader", node: SequenceNode) -> None:
        super().__init__()
        self.mode = node.value[0].value
        self.args = []
        for raw_node in node.value[1:]:
            self.args.append(loader.construct_object(raw_node))

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        args = []
        for arg in self.args:
            if isinstance(arg, YAMLTag):
                args.append(arg.resolve(entry, blueprint))
            else:
                args.append(arg)

        if not args:
            raise EntryInvalidError("At least one value is required after mode selection.")

        try:
            comparator = self._COMPARATORS[self.mode.upper()]
            return comparator(tuple(bool(x) for x in args))
        except (TypeError, KeyError) as exc:
            raise EntryInvalidError(exc)


class If(YAMLTag):
    """Select YAML to use based on condition"""

    condition: Any
    when_true: Any
    when_false: Any

    # pylint: disable=unused-argument
    def __init__(self, loader: "BlueprintLoader", node: SequenceNode) -> None:
        super().__init__()
        self.condition = loader.construct_object(node.value[0])
        self.when_true = loader.construct_object(node.value[1])
        self.when_false = loader.construct_object(node.value[2])

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        if isinstance(self.condition, YAMLTag):
            condition = self.condition.resolve(entry, blueprint)
        else:
            condition = self.condition

        try:
            return entry.tag_resolver(
                self.when_true if condition else self.when_false,
                blueprint,
            )
        except TypeError as exc:
            raise EntryInvalidError(exc)


class BaseForItem(YAMLTag):
    """Get the current item of a For tag context"""

    depth: int

    # pylint: disable=unused-argument
    def __init__(self, loader: "BlueprintLoader", node: ScalarNode) -> None:
        super().__init__()
        self.depth = int(node.value)

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        try:
            context_tag: For = entry._get_tag_context(depth=self.depth, context_tag_type=For)
        except ValueError:
            if self.depth == 0:
                raise EntryInvalidError(
                    f"{self.__class__.__name__} tags are only usable inside a For tag"
                )
            else:
                raise EntryInvalidError(
                    f"Invalid {self.__class__.__name__} tag depth: {self.depth}"
                )

        return context_tag.get_context(entry, blueprint)


class ForItemIndex(BaseForItem):
    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        return super().resolve(entry, blueprint)[0]


class ForItem(BaseForItem):
    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        return super().resolve(entry, blueprint)[1]


class For(YAMLTag, YAMLTagContext):
    """Iterate over an iterable."""

    iterable: YAMLTag | Iterable
    item_body: Any

    # pylint: disable=unused-argument
    def __init__(self, loader: "BlueprintLoader", node: SequenceNode) -> None:
        super().__init__()
        self.iterable = loader.construct_object(node.value[0])
        self.item_body = loader.construct_object(node.value[1])
        self.__current_context: Optional[Any] = None

    # pylint: disable=unused-argument
    def get_context(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        return self.__current_context

    def resolve(self, entry: BlueprintEntry, blueprint: Blueprint) -> Any:
        if isinstance(self.iterable, YAMLTag):
            iterable = self.iterable.resolve(entry, blueprint)
        else:
            iterable = self.iterable

        result = []

        self.__current_context = None

        for item in tuple(enumerate(iterable)):
            self.__current_context = item
            result.append(entry.tag_resolver(self.item_body, blueprint))

        self.__current_context = None

        return result


class BlueprintDumper(SafeDumper):
    """Dump dataclasses to yaml"""

    default_flow_style = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_representer(UUID, lambda self, data: self.represent_str(str(data)))
        self.add_representer(OrderedDict, lambda self, data: self.represent_dict(dict(data)))
        self.add_representer(Enum, lambda self, data: self.represent_str(data.value))
        self.add_representer(
            BlueprintEntryDesiredState, lambda self, data: self.represent_str(data.value)
        )
        self.add_representer(None, lambda self, data: self.represent_str(str(data)))

    def ignore_aliases(self, data):
        """Don't use any YAML anchors"""
        return True

    def represent(self, data) -> None:
        if is_dataclass(data):

            def factory(items):
                final_dict = dict(items)
                final_dict.pop("_state", None)
                return final_dict

            data = asdict(data, dict_factory=factory)
        return super().represent(data)


class BlueprintLoader(SafeLoader):
    """Loader for blueprints with custom tag support"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_constructor("!KeyOf", KeyOf)
        self.add_constructor("!Find", Find)
        self.add_constructor("!Context", Context)
        self.add_constructor("!Format", Format)
        self.add_constructor("!Condition", Condition)
        self.add_constructor("!If", If)
        self.add_constructor("!Env", Env)
        self.add_constructor("!For", For)
        self.add_constructor("!ForItem", ForItem)
        self.add_constructor("!ForItemIndex", ForItemIndex)


class EntryInvalidError(SentryIgnoredException):
    """Error raised when an entry is invalid"""

    serializer_errors: Optional[dict]

    def __init__(self, *args: object, serializer_errors: Optional[dict] = None) -> None:
        super().__init__(*args)
        self.serializer_errors = serializer_errors
