import logging
import os
import sys
import time
import warnings
from threading import local
from typing import Optional, Sequence
from urllib.parse import quote, unquote, urlparse

from neo4j import DEFAULT_DATABASE, GraphDatabase, basic_auth
from neo4j.api import Bookmarks
from neo4j.exceptions import ClientError, ServiceUnavailable, SessionExpired
from neo4j.graph import Node, Path, Relationship

from neomodel import config, core
from neomodel.exceptions import (
    ConstraintValidationFailed,
    FeatureNotSupported,
    NodeClassNotDefined,
    RelationshipClassNotDefined,
    UniqueProperty,
)

logger = logging.getLogger(__name__)


# make sure the connection url has been set prior to executing the wrapped function
def ensure_connection(func):
    def wrapper(self, *args, **kwargs):
        # Sort out where to find url
        if hasattr(self, "db"):
            _db = self.db
        else:
            _db = self

        if not _db.url:
            _db.set_connection(config.DATABASE_URL)

        return func(self, *args, **kwargs)

    return wrapper


class Database(local):
    """
    A singleton object via which all operations from neomodel to the Neo4j backend are handled with.
    """

    _NODE_CLASS_REGISTRY = {}

    def __init__(self):
        self._active_transaction = None
        self.url = None
        self.driver = None
        self._session = None
        self._pid = None
        self._database_name = DEFAULT_DATABASE
        self.protocol_version = None
        self._database_version = None
        self._database_edition = None
        self.impersonated_user = None

    def set_connection(self, url):
        """
        Sets the connection URL to the address a Neo4j server is set up at
        """
        p_start = url.replace(":", "", 1).find(":") + 2
        p_end = url.rfind("@")
        password = url[p_start:p_end]
        url = url.replace(password, quote(password))
        parsed_url = urlparse(url)

        valid_schemas = [
            "bolt",
            "bolt+s",
            "bolt+ssc",
            "bolt+routing",
            "neo4j",
            "neo4j+s",
            "neo4j+ssc",
        ]

        if parsed_url.netloc.find("@") > -1 and parsed_url.scheme in valid_schemas:
            credentials, hostname = parsed_url.netloc.rsplit("@", 1)
            username, password = credentials.split(":")
            password = unquote(password)
            database_name = parsed_url.path.strip("/")
        else:
            raise ValueError(
                f"Expecting url format: bolt://user:password@localhost:7687 got {url}"
            )

        options = {
            "auth": basic_auth(username, password),
            "connection_acquisition_timeout": config.CONNECTION_ACQUISITION_TIMEOUT,
            "connection_timeout": config.CONNECTION_TIMEOUT,
            "keep_alive": config.KEEP_ALIVE,
            "max_connection_lifetime": config.MAX_CONNECTION_LIFETIME,
            "max_connection_pool_size": config.MAX_CONNECTION_POOL_SIZE,
            "max_transaction_retry_time": config.MAX_TRANSACTION_RETRY_TIME,
            "resolver": config.RESOLVER,
            "user_agent": config.USER_AGENT,
        }

        if "+s" not in parsed_url.scheme:
            options["encrypted"] = config.ENCRYPTED
            options["trusted_certificates"] = config.TRUSTED_CERTIFICATES

        self.driver = GraphDatabase.driver(
            parsed_url.scheme + "://" + hostname, **options
        )
        self.url = url
        self._pid = os.getpid()
        self._active_transaction = None
        self._database_name = DEFAULT_DATABASE if database_name == "" else database_name

        # Getting the information about the database version requires a connection to the database
        self._database_version = None
        self._database_edition = None
        self._update_database_version()

    @property
    def database_version(self):
        if self._database_version is None:
            self._update_database_version()

        return self._database_version

    @property
    def database_edition(self):
        if self._database_edition is None:
            self._update_database_version()

        return self._database_edition

    @property
    def transaction(self):
        """
        Returns the current transaction object
        """
        return TransactionProxy(self)

    @property
    def write_transaction(self):
        return TransactionProxy(self, access_mode="WRITE")

    @property
    def read_transaction(self):
        return TransactionProxy(self, access_mode="READ")

    def impersonate(self, user: str) -> "ImpersonationHandler":
        """All queries executed within this context manager will be executed as impersonated user

        Args:
            user (str): User to impersonate

        Returns:
            ImpersonationHandler: Context manager to set/unset the user to impersonate
        """
        if self.database_edition != "enterprise":
            raise FeatureNotSupported(
                "Impersonation is only available in Neo4j Enterprise edition"
            )
        return ImpersonationHandler(self, impersonated_user=user)

    @ensure_connection
    def drop_constraints(self, quiet=True, io_stream=None):
        """
        Discover and drop all constraints.
        """
        if not io_stream or io_stream is None:
            io_stream = sys.stdout
    
        results, meta = self.cypher_query("SHOW CONSTRAINTS")
    
        results_as_dict = [dict(zip(meta, row)) for row in results]
        for constraint in results_as_dict:
            self.cypher_query(f"DROP CONSTRAINT {constraint['name']}")
            if not quiet:
                io_stream.write(
                    (
                        " - Dropping unique constraint and index"
                        f" on label {constraint['labelsOrTypes'][0]}"
                        f" with property {constraint['properties'][0]}.\n"
                    )
                )
        if not quiet:
            io_stream.write("\n")
    
    
    @ensure_connection
    def drop_indexes(quiet=True, io_stream=None):
        """
        Discover and drop all indexes, except the automatically created token lookup indexes.
        """
        if not io_stream or io_stream is None:
            io_stream = sys.stdout
    
        indexes = self.list_indexes(exclude_token_lookup=True)
        for index in indexes:
            self.cypher_query(f"DROP INDEX {index['name']}")
            if not quiet:
                io_stream.write(
                    f' - Dropping index on labels {",".join(index["labelsOrTypes"])} with properties {",".join(index["properties"])}.\n'
                )
        if not quiet:
            io_stream.write("\n")

    @ensure_connection
    def remove_all_labels(quiet=True, io_stream=None):
        """
        Calls functions for dropping constraints and indexes.
    
        :param io_stream: output stream
        :return: None
        """
        io_stream = io_stream or sys.stdout

        io_stream.write("Dropping constraints...\n")
        self.drop_constraints(quiet, io_stream)
    
        io_stream.write("Dropping indexes...\n")
        self.drop_indexes(quiet, io_stream)


    def remove_all_labels(stdout=None):
        """
        Calls functions for dropping constraints and indexes.
    
        :param stdout: output stream
        :return: None
        """
    
        if not stdout:
            stdout = sys.stdout
    
        stdout.write("Dropping constraints...\n")
        drop_constraints(quiet=False, stdout=stdout)
    
        stdout.write("Dropping indexes...\n")
        drop_indexes(quiet=False, stdout=stdout)
    
    
    def install_labels(cls, quiet=True, stdout=None):
        """
        Setup labels with indexes and constraints for a given class
    
        :param cls: StructuredNode class
        :type: class
        :param quiet: (default true) enable standard output
        :param stdout: stdout stream
        :type: bool
        :return: None
        """
        if not stdout or stdout is None:
            stdout = sys.stdout
    
        if not hasattr(cls, "__label__"):
            if not quiet:
                stdout.write(
                    f" ! Skipping class {cls.__module__}.{cls.__name__} is abstract\n"
                )
            return
    
        for name, property in cls.defined_properties(aliases=False, rels=False).items():
            _install_node(cls, name, property, quiet, stdout)
    
        for _, relationship in cls.defined_properties(
            aliases=False, rels=True, properties=False
        ).items():
            _install_relationship(cls, relationship, quiet, stdout)

    
    def _install_node(cls, name, property, quiet, stdout):

        def _create_node_index(label: str, property_name: str, stdout):
            try:
                db.cypher_query(
                    f"CREATE INDEX index_{label}_{property_name} FOR (n:{label}) ON (n.{property_name}); "
                )
            except ClientError as e:
                if e.code in (
                    RULE_ALREADY_EXISTS,
                    INDEX_ALREADY_EXISTS,
                ):
                    stdout.write(f"{str(e)}\n")
                else:
                    raise
        
        
        def _create_node_constraint(label: str, property_name: str, stdout):
            try:
                db.cypher_query(
                    f"""CREATE CONSTRAINT constraint_unique_{label}_{property_name} 
                                FOR (n:{label}) REQUIRE n.{property_name} IS UNIQUE"""
                )
            except ClientError as e:
                if e.code in (
                    RULE_ALREADY_EXISTS,
                    CONSTRAINT_ALREADY_EXISTS,
                ):
                    stdout.write(f"{str(e)}\n")
                else:
                    raise

        # Create indexes and constraints for node property
        db_property = property.db_property or name
        if property.index:
            if not quiet:
                stdout.write(
                    f" + Creating node index {name} on label {cls.__label__} for class {cls.__module__}.{cls.__name__}\n"
                )
            _create_node_index(
                label=cls.__label__, property_name=db_property, stdout=stdout
            )
    
        elif property.unique_index:
            if not quiet:
                stdout.write(
                    f" + Creating node unique constraint for {name} on label {cls.__label__} for class {cls.__module__}.{cls.__name__}\n"
                )
            _create_node_constraint(
                label=cls.__label__, property_name=db_property, stdout=stdout
            )
    
    
    def _install_relationship(cls, relationship, quiet, stdout):
        def _create_relationship_index(relationship_type: str, property_name: str, stdout):
            try:
                db.cypher_query(
                    f"CREATE INDEX index_{relationship_type}_{property_name} FOR ()-[r:{relationship_type}]-() ON (r.{property_name}); "
                )
            except ClientError as e:
                if e.code in (
                    RULE_ALREADY_EXISTS,
                    INDEX_ALREADY_EXISTS,
                ):
                    stdout.write(f"{str(e)}\n")
                else:
                    raise

        # Create indexes and constraints for relationship property
        relationship_cls = relationship.definition["model"]
        if relationship_cls is not None:
            relationship_type = relationship.definition["relation_type"]
            for prop_name, property in relationship_cls.defined_properties(
                aliases=False, rels=False
            ).items():
                db_property = property.db_property or prop_name
                if property.index:
                    if not quiet:
                        stdout.write(
                            f" + Creating relationship index {prop_name} on relationship type {relationship_type} for relationship model {cls.__module__}.{relationship_cls.__name__}\n"
                        )
                    _create_relationship_index(
                        relationship_type=relationship_type,
                        property_name=db_property,
                        stdout=stdout,
                    )
    
    
    def install_all_labels(stdout=None):
        """
        Discover all subclasses of StructuredNode in your application and execute install_labels on each.
        Note: code must be loaded (imported) in order for a class to be discovered.
    
        :param stdout: output stream
        :return: None
        """
    
        if not stdout or stdout is None:
            stdout = sys.stdout
    
        def subsub(cls):  # recursively return all subclasses
            subclasses = cls.__subclasses__()
            if not subclasses:  # base case: no more subclasses
                return []
            return subclasses + [g for s in cls.__subclasses__() for g in subsub(s)]
    
        stdout.write("Setting up indexes and constraints...\n\n")
    
        i = 0
        for cls in subsub(StructuredNode):
            stdout.write(f"Found {cls.__module__}.{cls.__name__}\n")
            install_labels(cls, quiet=False, stdout=stdout)
            i += 1
    
        if i:
            stdout.write("\n")
    
        stdout.write(f"Finished {i} classes.\n")



    @ensure_connection
    def change_neo4j_password(self, user, new_password):
        self.cypher_query(f"ALTER USER {user} SET PASSWORD '{new_password}'")

    @ensure_connection
    def clear_neo4j_database(self, clear_constraints=False, clear_indexes=False):
        self.cypher_query(
            """
            MATCH (a)
            CALL { WITH a DETACH DELETE a }
            IN TRANSACTIONS OF 5000 rows
            """
            )
        if clear_constraints:
            self.drop_constraints()

        if clear_indexes:
            self.drop_indexes()

    @ensure_connection
    def begin(self, access_mode=None, **parameters):
        """
        Begins a new transaction. Raises SystemError if a transaction is already active.
        """
        if (
            hasattr(self, "_active_transaction")
            and self._active_transaction is not None
        ):
            raise SystemError("Transaction in progress")
        self._session = self.driver.session(
            default_access_mode=access_mode,
            database=self._database_name,
            impersonated_user=self.impersonated_user,
            **parameters,
        )
        self._active_transaction = self._session.begin_transaction()

    @ensure_connection
    def commit(self):
        """
        Commits the current transaction and closes its session

        :return: last_bookmarks
        """
        try:
            self._active_transaction.commit()
            last_bookmarks = self._session.last_bookmarks()
        finally:
            # In case when something went wrong during
            # committing changes to the database
            # we have to close an active transaction and session.
            self._active_transaction.close()
            self._session.close()
            self._active_transaction = None
            self._session = None

        return last_bookmarks

    @ensure_connection
    def rollback(self):
        """
        Rolls back the current transaction and closes its session
        """
        try:
            self._active_transaction.rollback()
        finally:
            # In case when something went wrong during changes rollback,
            # we have to close an active transaction and session
            self._active_transaction.close()
            self._session.close()
            self._active_transaction = None
            self._session = None

    def _update_database_version(self):
        """
        Updates the database server information when it is required
        """
        try:
            results = self.cypher_query(
                "CALL dbms.components() yield versions, edition return versions[0], edition"
            )
            self._database_version = results[0][0][0]
            self._database_edition = results[0][0][1]
        except ServiceUnavailable:
            # The database server is not running yet
            pass

    def _object_resolution(self, object_to_resolve):
        """
        Performs in place automatic object resolution on a result
        returned by cypher_query.

        The function operates recursively in order to be able to resolve Nodes
        within nested list structures and Path objects. Not meant to be called
        directly, used primarily by _result_resolution.

        :param object_to_resolve: A result as returned by cypher_query.
        :type Any:

        :return: An instantiated object.
        """
        # Below is the original comment that came with the code extracted in
        # this method. It is not very clear but I decided to keep it just in
        # case
        #
        #
        # For some reason, while the type of `a_result_attribute[1]`
        # as reported by the neo4j driver is `Node` for Node-type data
        # retrieved from the database.
        # When the retrieved data are Relationship-Type,
        # the returned type is `abc.[REL_LABEL]` which is however
        # a descendant of Relationship.
        # Consequently, the type checking was changed for both
        # Node, Relationship objects
        if isinstance(object_to_resolve, Node):
            return self._NODE_CLASS_REGISTRY[
                frozenset(object_to_resolve.labels)
            ].inflate(object_to_resolve)

        if isinstance(object_to_resolve, Relationship):
            rel_type = frozenset([object_to_resolve.type])
            return self._NODE_CLASS_REGISTRY[rel_type].inflate(object_to_resolve)

        if isinstance(object_to_resolve, Path):
            from .path import NeomodelPath

            return NeomodelPath(object_to_resolve)

        if isinstance(object_to_resolve, list):
            return self._result_resolution([object_to_resolve])

        return object_to_resolve

    def _result_resolution(self, result_list):
        """
        Performs in place automatic object resolution on a set of results
        returned by cypher_query.

        The function operates recursively in order to be able to resolve Nodes
        within nested list structures. Not meant to be called directly,
        used primarily by cypher_query.

        :param result_list: A list of results as returned by cypher_query.
        :type list:

        :return: A list of instantiated objects.
        """

        # Object resolution occurs in-place
        for a_result_item in enumerate(result_list):
            for a_result_attribute in enumerate(a_result_item[1]):
                try:
                    # Primitive types should remain primitive types,
                    # Nodes to be resolved to native objects
                    resolved_object = a_result_attribute[1]

                    resolved_object = self._object_resolution(resolved_object)

                    result_list[a_result_item[0]][
                        a_result_attribute[0]
                    ] = resolved_object

                except KeyError as exc:
                    # Not being able to match the label set of a node with a known object results
                    # in a KeyError in the internal dictionary used for resolution. If it is impossible
                    # to match, then raise an exception with more details about the error.
                    if isinstance(a_result_attribute[1], Node):
                        raise NodeClassNotDefined(
                            a_result_attribute[1], self._NODE_CLASS_REGISTRY
                        ) from exc

                    if isinstance(a_result_attribute[1], Relationship):
                        raise RelationshipClassNotDefined(
                            a_result_attribute[1], self._NODE_CLASS_REGISTRY
                        ) from exc

        return result_list

    @ensure_connection
    def cypher_query(
        self,
        query,
        params=None,
        handle_unique=True,
        retry_on_session_expire=False,
        resolve_objects=False,
    ):
        """
        Runs a query on the database and returns a list of results and their headers.

        :param query: A CYPHER query
        :type: str
        :param params: Dictionary of parameters
        :type: dict
        :param handle_unique: Whether or not to raise UniqueProperty exception on Cypher's ConstraintValidation errors
        :type: bool
        :param retry_on_session_expire: Whether or not to attempt the same query again if the transaction has expired
        :type: bool
        :param resolve_objects: Whether to attempt to resolve the returned nodes to data model objects automatically
        :type: bool
        """

        if self._active_transaction:
            # Use current session is a transaction is currently active
            results, meta = self._run_cypher_query(
                self._active_transaction,
                query,
                params,
                handle_unique,
                retry_on_session_expire,
                resolve_objects,
            )
        else:
            # Otherwise create a new session in a with to dispose of it after it has been run
            with self.driver.session(
                database=self._database_name, impersonated_user=self.impersonated_user
            ) as session:
                results, meta = self._run_cypher_query(
                    session,
                    query,
                    params,
                    handle_unique,
                    retry_on_session_expire,
                    resolve_objects,
                )

        return results, meta

    def _run_cypher_query(
        self,
        session,
        query,
        params,
        handle_unique,
        retry_on_session_expire,
        resolve_objects,
    ):
        try:
            # Retrieve the data
            start = time.time()
            response = session.run(query, params)
            results, meta = [list(r.values()) for r in response], response.keys()
            end = time.time()

            if resolve_objects:
                # Do any automatic resolution required
                results = self._result_resolution(results)

        except ClientError as e:
            if e.code == "Neo.ClientError.Schema.ConstraintValidationFailed":
                if "already exists with label" in e.message and handle_unique:
                    raise UniqueProperty(e.message) from e

                raise ConstraintValidationFailed(e.message) from e
            exc_info = sys.exc_info()
            raise exc_info[1].with_traceback(exc_info[2])
        except SessionExpired:
            if retry_on_session_expire:
                self.set_connection(self.url)
                return self.cypher_query(
                    query=query,
                    params=params,
                    handle_unique=handle_unique,
                    retry_on_session_expire=False,
                )
            raise

        tte = end - start
        if os.environ.get("NEOMODEL_CYPHER_DEBUG", False) and tte > float(
            os.environ.get("NEOMODEL_SLOW_QUERIES", 0)
        ):
            logger.debug(
                "query: "
                + query
                + "\nparams: "
                + repr(params)
                + f"\ntook: {tte:.2g}s\n"
            )

        return results, meta

    def get_id_method(self) -> str:
        if self.database_version.startswith("4"):
            return "id"
        else:
            return "elementId"

    def list_indexes(self, exclude_token_lookup=False) -> Sequence[dict]:
        """Returns all indexes existing in the database

        Arguments:
            exclude_token_lookup[bool]: Exclude automatically create token lookup indexes

        Returns:
            Sequence[dict]: List of dictionaries, each entry being an index definition
        """
        indexes, meta_indexes = self.cypher_query("SHOW INDEXES")
        indexes_as_dict = [dict(zip(meta_indexes, row)) for row in indexes]

        if exclude_token_lookup:
            indexes_as_dict = [
                obj for obj in indexes_as_dict if obj["type"] != "LOOKUP"
            ]

        return indexes_as_dict

    def list_constraints(self) -> Sequence[dict]:
        """Returns all constraints existing in the database

        Returns:
            Sequence[dict]: List of dictionaries, each entry being a constraint definition
        """
        constraints, meta_constraints = self.cypher_query("SHOW CONSTRAINTS")
        constraints_as_dict = [dict(zip(meta_constraints, row)) for row in constraints]

        return constraints_as_dict


class TransactionProxy:
    bookmarks: Optional[Bookmarks] = None

    def __init__(self, db, access_mode=None):
        self.db = db
        self.access_mode = access_mode

    @ensure_connection
    def __enter__(self):
        self.db.begin(access_mode=self.access_mode, bookmarks=self.bookmarks)
        self.bookmarks = None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value:
            self.db.rollback()

        if (
            exc_type is ClientError
            and exc_value.code == "Neo.ClientError.Schema.ConstraintValidationFailed"
        ):
            raise UniqueProperty(exc_value.message)

        if not exc_value:
            self.last_bookmark = self.db.commit()

    def __call__(self, func):
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper

    @property
    def with_bookmark(self):
        return BookmarkingTransactionProxy(self.db, self.access_mode)


class ImpersonationHandler:
    def __init__(self, db, impersonated_user: str):
        self.db = db
        self.impersonated_user = impersonated_user

    def __enter__(self):
        self.db.impersonated_user = self.impersonated_user
        return self

    def __exit__(self, exception_type, exception_value, exception_traceback):
        self.db.impersonated_user = None

        print("\nException type:", exception_type)
        print("\nException value:", exception_value)
        print("\nTraceback:", exception_traceback)

    def __call__(self, func):
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper


class BookmarkingTransactionProxy(TransactionProxy):
    def __call__(self, func):
        def wrapper(*args, **kwargs):
            self.bookmarks = kwargs.pop("bookmarks", None)

            with self:
                result = func(*args, **kwargs)
                self.last_bookmark = None

            return result, self.last_bookmark

        return wrapper


def deprecated(message):
    # pylint:disable=invalid-name
    def f__(f):
        def f_(*args, **kwargs):
            warnings.warn(message, category=DeprecationWarning, stacklevel=2)
            return f(*args, **kwargs)

        f_.__name__ = f.__name__
        f_.__doc__ = f.__doc__
        f_.__dict__.update(f.__dict__)
        return f_

    return f__


def classproperty(f):
    class cpf:
        def __init__(self, getter):
            self.getter = getter

        def __get__(self, obj, type=None):
            return self.getter(type)

    return cpf(f)


# Just used for error messages
class _UnsavedNode:
    def __repr__(self):
        return "<unsaved node>"

    def __str__(self):
        return self.__repr__()


def _get_node_properties(node):
    """Get the properties from a neo4j.vx.types.graph.Node object."""
    return node._properties


def enumerate_traceback(initial_frame):
    depth, frame = 0, initial_frame
    while frame is not None:
        yield depth, frame
        frame = frame.f_back
        depth += 1
