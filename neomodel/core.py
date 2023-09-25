import sys
import warnings
from itertools import combinations

from neo4j.exceptions import ClientError

from neomodel import config
from neomodel.exceptions import DoesNotExist, NodeClassAlreadyDefined
from neomodel.hooks import hooks
from neomodel.properties import Property, PropertyManager
from neomodel.util import Database, _get_node_properties, _UnsavedNode, classproperty

db = Database()

RULE_ALREADY_EXISTS = "Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists"
INDEX_ALREADY_EXISTS = "Neo.ClientError.Schema.IndexAlreadyExists"
CONSTRAINT_ALREADY_EXISTS = "Neo.ClientError.Schema.ConstraintAlreadyExists"
STREAMING_WARNING = "streaming is not supported by bolt, please remove the kwarg"

class NodeMeta(type):
    def __new__(mcs, name, bases, namespace):
        namespace["DoesNotExist"] = type(name + "DoesNotExist", (DoesNotExist,), {})
        cls = super().__new__(mcs, name, bases, namespace)
        cls.DoesNotExist._model_class = cls

        if hasattr(cls, "__abstract_node__"):
            delattr(cls, "__abstract_node__")
        else:
            if "deleted" in namespace:
                raise ValueError(
                    "Class property called 'deleted' conflicts "
                    "with neomodel internals."
                )
            for key, value in (
                (x, y) for x, y in namespace.items() if isinstance(y, Property)
            ):
                value.name, value.owner = key, cls
                if hasattr(value, "setup") and callable(value.setup):
                    value.setup()

            # cache various groups of properies
            cls.__required_properties__ = tuple(
                name
                for name, property in cls.defined_properties(
                    aliases=False, rels=False
                ).items()
                if property.required or property.unique_index
            )
            cls.__all_properties__ = tuple(
                cls.defined_properties(aliases=False, rels=False).items()
            )
            cls.__all_aliases__ = tuple(
                cls.defined_properties(properties=False, rels=False).items()
            )
            cls.__all_relationships__ = tuple(
                cls.defined_properties(aliases=False, properties=False).items()
            )

            cls.__label__ = namespace.get("__label__", name)
            cls.__optional_labels__ = namespace.get("__optional_labels__", [])

            if config.AUTO_INSTALL_LABELS:
                install_labels(cls, quiet=False)

            build_class_registry(cls)

        return cls


def build_class_registry(cls):
    base_label_set = frozenset(cls.inherited_labels())
    optional_label_set = set(cls.inherited_optional_labels())

    # Construct all possible combinations of labels + optional labels
    possible_label_combinations = [
        frozenset(set(x).union(base_label_set))
        for i in range(1, len(optional_label_set) + 1)
        for x in combinations(optional_label_set, i)
    ]
    possible_label_combinations.append(base_label_set)

    for label_set in possible_label_combinations:
        if label_set not in db._NODE_CLASS_REGISTRY:
            db._NODE_CLASS_REGISTRY[label_set] = cls
        else:
            raise NodeClassAlreadyDefined(cls, db._NODE_CLASS_REGISTRY)


NodeBase = NodeMeta("NodeBase", (PropertyManager,), {"__abstract_node__": True})


class StructuredNode(NodeBase):
    """
    Base class for all node definitions to inherit from.

    If you want to create your own abstract classes set:
        __abstract_node__ = True
    """

    # static properties

    __abstract_node__ = True

    # magic methods

    def __init__(self, *args, **kwargs):
        if "deleted" in kwargs:
            raise ValueError("deleted property is reserved for neomodel")

        for key, val in self.__all_relationships__:
            self.__dict__[key] = val.build_manager(self, key)

        super().__init__(*args, **kwargs)

    def __eq__(self, other):
        if not isinstance(other, (StructuredNode,)):
            return False
        if hasattr(self, "element_id") and hasattr(other, "element_id"):
            return self.element_id == other.element_id
        return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return f"<{self.__class__.__name__}: {self}>"

    def __str__(self):
        return repr(self.__properties__)

    # dynamic properties

    @classproperty
    def nodes(cls):
        """
        Returns a NodeSet object representing all nodes of the classes label
        :return: NodeSet
        :rtype: NodeSet
        """
        from .match import NodeSet

        return NodeSet(cls)

    @property
    def element_id(self):
        return (
            int(self.element_id_property)
            if db.database_version.startswith("4")
            else self.element_id_property
        )

    # Version 4.4 support - id is deprecated in version 5.x
    @property
    def id(self):
        try:
            return int(self.element_id_property)
        except (TypeError, ValueError):
            raise ValueError(
                "id is deprecated in Neo4j version 5, please migrate to element_id. If you use the id in a Cypher query, replace id() by elementId()."
            )

    # methods

    @classmethod
    def _build_merge_query(
        cls, merge_params, update_existing=False, lazy=False, relationship=None
    ):
        """
        Get a tuple of a CYPHER query and a params dict for the specified MERGE query.

        :param merge_params: The target node match parameters, each node must have a "create" key and optional "update".
        :type merge_params: list of dict
        :param update_existing: True to update properties of existing nodes, default False to keep existing values.
        :type update_existing: bool
        :rtype: tuple
        """
        query_params = dict(merge_params=merge_params)
        n_merge_labels = ":".join(cls.inherited_labels())
        n_merge_prm = ", ".join(
            (
                f"{getattr(cls, p).db_property or p}: params.create.{getattr(cls, p).db_property or p}"
                for p in cls.__required_properties__
            )
        )
        n_merge = f"n:{n_merge_labels} {{{n_merge_prm}}}"
        if relationship is None:
            # create "simple" unwind query
            query = f"UNWIND $merge_params as params\n MERGE ({n_merge})\n "
        else:
            # validate relationship
            if not isinstance(relationship.source, StructuredNode):
                raise ValueError(
                    f"relationship source [{repr(relationship.source)}] is not a StructuredNode"
                )
            relation_type = relationship.definition.get("relation_type")
            if not relation_type:
                raise ValueError(
                    "No relation_type is specified on provided relationship"
                )

            from .match import _rel_helper

            query_params["source_id"] = relationship.source.element_id
            query = f"MATCH (source:{relationship.source.__label__}) WHERE {db.get_id_method()}(source) = $source_id\n "
            query += "WITH source\n UNWIND $merge_params as params \n "
            query += "MERGE "
            query += _rel_helper(
                lhs="source",
                rhs=n_merge,
                ident=None,
                relation_type=relation_type,
                direction=relationship.definition["direction"],
            )

        query += "ON CREATE SET n = params.create\n "
        # if update_existing, write properties on match as well
        if update_existing is True:
            query += "ON MATCH SET n += params.update\n"

        # close query
        if lazy:
            query += f"RETURN {db.get_id_method()}(n)"
        else:
            query += "RETURN n"

        return query, query_params

    @classmethod
    def create(cls, *props, **kwargs):
        """
        Call to CREATE with parameters map. A new instance will be created and saved.

        :param props: dict of properties to create the nodes.
        :type props: tuple
        :param lazy: False by default, specify True to get nodes with id only without the parameters.
        :type: bool
        :rtype: list
        """

        if "streaming" in kwargs:
            warnings.warn(
                STREAMING_WARNING,
                category=DeprecationWarning,
                stacklevel=1,
            )

        lazy = kwargs.get("lazy", False)
        # create mapped query
        query = f"CREATE (n:{':'.join(cls.inherited_labels())} $create_params)"

        # close query
        if lazy:
            query += f" RETURN {db.get_id_method()}(n)"
        else:
            query += " RETURN n"

        results = []
        for item in [
            cls.deflate(p, obj=_UnsavedNode(), skip_empty=True) for p in props
        ]:
            node, _ = db.cypher_query(query, {"create_params": item})
            results.extend(node[0])

        nodes = [cls.inflate(node) for node in results]

        if not lazy and hasattr(cls, "post_create"):
            for node in nodes:
                node.post_create()

        return nodes

    @classmethod
    def create_or_update(cls, *props, **kwargs):
        """
        Call to MERGE with parameters map. A new instance will be created and saved if does not already exists,
        this is an atomic operation. If an instance already exists all optional properties specified will be updated.

        Note that the post_create hook isn't called after create_or_update

        :param props: List of dict arguments to get or create the entities with.
        :type props: tuple
        :param relationship: Optional, relationship to get/create on when new entity is created.
        :param lazy: False by default, specify True to get nodes with id only without the parameters.
        :rtype: list
        """
        lazy = kwargs.get("lazy", False)
        relationship = kwargs.get("relationship")

        # build merge query, make sure to update only explicitly specified properties
        create_or_update_params = []
        for specified, deflated in [
            (p, cls.deflate(p, skip_empty=True)) for p in props
        ]:
            create_or_update_params.append(
                {
                    "create": deflated,
                    "update": dict(
                        (k, v) for k, v in deflated.items() if k in specified
                    ),
                }
            )
        query, params = cls._build_merge_query(
            create_or_update_params,
            update_existing=True,
            relationship=relationship,
            lazy=lazy,
        )

        if "streaming" in kwargs:
            warnings.warn(
                STREAMING_WARNING,
                category=DeprecationWarning,
                stacklevel=1,
            )

        # fetch and build instance for each result
        results = db.cypher_query(query, params)
        return [cls.inflate(r[0]) for r in results[0]]

    def cypher(self, query, params=None):
        """
        Execute a cypher query with the param 'self' pre-populated with the nodes neo4j id.

        :param query: cypher query string
        :type: string
        :param params: query parameters
        :type: dict
        :return: list containing query results
        :rtype: list
        """
        self._pre_action_check("cypher")
        params = params or {}
        params.update({"self": self.element_id})
        return db.cypher_query(query, params)

    @hooks
    def delete(self):
        """
        Delete a node and its relationships

        :return: True
        """
        self._pre_action_check("delete")
        self.cypher(
            f"MATCH (self) WHERE {db.get_id_method()}(self)=$self DETACH DELETE self"
        )
        delattr(self, "element_id_property")
        self.deleted = True
        return True

    @classmethod
    def get_or_create(cls, *props, **kwargs):
        """
        Call to MERGE with parameters map. A new instance will be created and saved if does not already exist,
        this is an atomic operation.
        Parameters must contain all required properties, any non required properties with defaults will be generated.

        Note that the post_create hook isn't called after get_or_create

        :param props: Arguments to get_or_create as tuple of dict with property names and values to get or create
                      the entities with.
        :type props: tuple
        :param relationship: Optional, relationship to get/create on when new entity is created.
        :param lazy: False by default, specify True to get nodes with id only without the parameters.
        :rtype: list
        """
        lazy = kwargs.get("lazy", False)
        relationship = kwargs.get("relationship")

        # build merge query
        get_or_create_params = [
            {"create": cls.deflate(p, skip_empty=True)} for p in props
        ]
        query, params = cls._build_merge_query(
            get_or_create_params, relationship=relationship, lazy=lazy
        )

        if "streaming" in kwargs:
            warnings.warn(
                STREAMING_WARNING,
                category=DeprecationWarning,
                stacklevel=1,
            )

        # fetch and build instance for each result
        results = db.cypher_query(query, params)
        return [cls.inflate(r[0]) for r in results[0]]

    @classmethod
    def inflate(cls, node):
        """
        Inflate a raw neo4j_driver node to a neomodel node
        :param node:
        :return: node object
        """
        # support lazy loading
        if isinstance(node, str) or isinstance(node, int):
            snode = cls()
            snode.element_id_property = node
        else:
            node_properties = _get_node_properties(node)
            props = {}
            for key, prop in cls.__all_properties__:
                # map property name from database to object property
                db_property = prop.db_property or key

                if db_property in node_properties:
                    props[key] = prop.inflate(node_properties[db_property], node)
                elif prop.has_default:
                    props[key] = prop.default_value()
                else:
                    props[key] = None

            snode = cls(**props)
            snode.element_id_property = node.element_id

        return snode

    @classmethod
    def inherited_labels(cls):
        """
        Return list of labels from nodes class hierarchy.

        :return: list
        """
        return [
            scls.__label__
            for scls in cls.mro()
            if hasattr(scls, "__label__") and not hasattr(scls, "__abstract_node__")
        ]

    @classmethod
    def inherited_optional_labels(cls):
        """
        Return list of optional labels from nodes class hierarchy.

        :return: list
        :rtype: list
        """
        return [
            label
            for scls in cls.mro()
            for label in getattr(scls, "__optional_labels__", [])
            if not hasattr(scls, "__abstract_node__")
        ]

    def labels(self):
        """
        Returns list of labels tied to the node from neo4j.

        :return: list of labels
        :rtype: list
        """
        self._pre_action_check("labels")
        return self.cypher(
            f"MATCH (n) WHERE {db.get_id_method()}(n)=$self " "RETURN labels(n)"
        )[0][0][0]

    def _pre_action_check(self, action):
        if hasattr(self, "deleted") and self.deleted:
            raise ValueError(
                f"{self.__class__.__name__}.{action}() attempted on deleted node"
            )
        if not hasattr(self, "element_id"):
            raise ValueError(
                f"{self.__class__.__name__}.{action}() attempted on unsaved node"
            )

    def refresh(self):
        """
        Reload the node from neo4j
        """
        self._pre_action_check("refresh")
        if hasattr(self, "element_id"):
            request = self.cypher(
                f"MATCH (n) WHERE {db.get_id_method()}(n)=$self RETURN n"
            )[0]
            if not request or not request[0]:
                raise self.__class__.DoesNotExist("Can't refresh non existent node")
            node = self.inflate(request[0][0])
            for key, val in node.__properties__.items():
                setattr(self, key, val)
        else:
            raise ValueError("Can't refresh unsaved node")

    @hooks
    def save(self):
        """
        Save the node to neo4j or raise an exception

        :return: the node instance
        """

        # create or update instance node
        if hasattr(self, "element_id"):
            # update
            params = self.deflate(self.__properties__, self)
            query = f"MATCH (n) WHERE {db.get_id_method()}(n)=$self\n"

            if params:
                query += "SET "
                query += ",\n".join([f"n.{key} = ${key}" for key in params])
                query += "\n"
            if self.inherited_labels():
                query += "\n".join(
                    [f"SET n:`{label}`" for label in self.inherited_labels()]
                )
            self.cypher(query, params)
        elif hasattr(self, "deleted") and self.deleted:
            raise ValueError(
                f"{self.__class__.__name__}.save() attempted on deleted node"
            )
        else:  # create
            created_node = self.create(self.__properties__)[0]
            self.element_id_property = created_node.element_id
        return self
