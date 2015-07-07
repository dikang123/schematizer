# -*- coding: utf-8 -*-
import uuid

import simplejson
from sqlalchemy import exc
from sqlalchemy.orm import exc as orm_exc

from schematizer import models
from schematizer.components.converters.converter_base import BaseConverter
from schematizer.logic.schema_resolution import SchemaCompatibilityValidator
from schematizer.models.database import session


def is_backward_compatible(old_schema_json, new_schema_json):
    """Whether the data serialized using specified old_schema_json can be
    deserialized using specified new_schema_json.
    """
    return SchemaCompatibilityValidator.is_backward_compatible(
        old_schema_json,
        new_schema_json
    )


def is_forward_compatible(old_schema_json, new_schema_json):
    """Whether the data serialized using specified new_schema_json can be
    deserialized using specified old_schema_json.
    """
    return SchemaCompatibilityValidator.is_backward_compatible(
        new_schema_json,
        old_schema_json
    )


def is_full_compatible(old_schema_json, new_schema_json):
    """Whether the data serialized using specified old_schema_json can be
    deserialized using specified new_schema_json, and vice versa.
    """
    return (is_backward_compatible(old_schema_json, new_schema_json) and
            is_forward_compatible(old_schema_json, new_schema_json))


class EntityNotFoundException(Exception):
    pass


class IncompatibleSchemaException(Exception):
    pass


class MissingDocException(Exception):
    pass


def load_converters():
    __import__('schematizer.components.converters', fromlist=['converters'])
    _converters = dict()
    for cls in BaseConverter.__subclasses__():
        _converters[(cls.source_type, cls.target_type)] = cls
    return _converters


converters = load_converters()


def convert_schema(source_type, target_type, source_schema):
    """Convert the source type schema to the target type schema. The
    source_type and target_type are the SchemaKindEnum.
    """
    converter = converters.get((source_type, target_type))
    if not converter:
        raise Exception("Unable to find converter to convert from {0} to {1}."
                        .format(source_type, target_type))
    return converter().convert(source_schema)


def create_avro_schema_from_avro_json(
        avro_schema_json,
        namespace,
        source,
        domain_owner_email,
        status=models.AvroSchemaStatus.READ_AND_WRITE,
        base_schema_id=None
):
    """Add an Avro schema of given schema json object into schema store.
    The steps from checking compatibility to create new topic should be atomic.

    :param avro_schema_json: JSON representation of Avro schema
    :param namespace: namespace string
    :param source: source name string
    :param domain_owner_email: email of the schema owner
    :param status: AvroStatusEnum: RW/R/Disabled
    :param base_schema_id: Id of the Avro schema from which the new schema is
    derived from
    :return: New created AvroSchema object.
    """
    is_valid, error = models.AvroSchema.verify_avro_schema(avro_schema_json)
    if not is_valid:
        raise ValueError("Invalid Avro schema JSON. Value: {0}. Error: {1}"
                         .format(avro_schema_json, error))

    domain = _get_domain_or_create(namespace, source, domain_owner_email)

    # Lock the domain so that no other transaction can add new topic to it.
    _lock_domain(domain)

    topic = get_latest_topic_of_domain(namespace, source)

    # Lock topic and its schemas so that no other transaction can add new
    # schema to the topic or change schema status.
    _lock_topic_and_schemas(topic)

    if (not topic or not is_schema_compatible_in_topic(
            avro_schema_json,
            topic.name
    )):
        # Note that creating duplicate topic names will throw a sqlalchemy
        # IntegrityError exception. When it occurs, it indicates the uuid
        # is generating the same value (rarely) and we'd like to know it.
        topic_name = _construct_topic_name(namespace, source)
        topic = _create_topic(topic_name, domain)

    # Do not create the schema if it is the same as the latest one
    latest_schema = get_latest_schema_by_topic_id(topic.id)
    if (latest_schema and
            latest_schema.avro_schema_json == avro_schema_json and
            latest_schema.base_schema_id == base_schema_id):
        return latest_schema

    avro_schema = _create_avro_schema(
        avro_schema_json,
        topic.id,
        status,
        base_schema_id
    )
    return avro_schema


def _get_domain_or_create(namespace, source, owner_email):
    try:
        return session.query(
            models.Domain
        ).filter(
            models.Domain.namespace == namespace,
            models.Domain.source == source
        ).one()
    except orm_exc.NoResultFound:
        return _create_domain_if_not_exist(namespace, source, owner_email)


def _create_domain_if_not_exist(namespace, source, owner_email):
    try:
        # Create a savepoint before trying to create new domain so that
        # in the case which the IntegrityError occurs, the session will
        # rollback to savepoint. Upon exiting the nested context, commit/
        # rollback is automatically issued and no need to add it explicitly.
        with session.begin_nested():
            domain = models.Domain(
                namespace=namespace,
                source=source,
                owner_email=owner_email
            )
            session.add(domain)
    except exc.IntegrityError:
        # Ignore this error due to trying to create a duplicate domain
        # (same namespace and source). Simply get the existing one.
        domain = get_domain_by_fullname(namespace, source)
    return domain


def _lock_domain(domain):
    session.query(
        models.Domain
    ).filter(
        models.Domain.id == domain.id
    ).with_for_update()


def _lock_topic_and_schemas(topic):
    if not topic:
        return
    session.query(
        models.Topic
    ).filter(
        models.Topic.id == topic.id
    ).with_for_update()
    session.query(
        models.AvroSchema
    ).filter(
        models.AvroSchema.topic_id == topic.id
    ).with_for_update()


def get_latest_topic_of_domain(namespace, source):
    """Get the latest topic of given namespace and source. The latest one is
    the one created most recently. It returns None if no such topic exists.
    """
    domain = get_domain_by_fullname(namespace, source)
    if not domain:
        raise EntityNotFoundException(
            "Cannot find namespace {0} source {1}.".format(namespace, source)
        )

    return session.query(
        models.Topic
    ).filter(
        models.Topic.domain_id == domain.id
    ).order_by(
        models.Topic.id.desc()
    ).first()


def is_schema_compatible_in_topic(target_schema, topic_name):
    """Check whether given schema is a valid Avro schema and compatible
    with existing schemas in the specified topic. Note that target_schema
    is the avro json object.
    """
    enabled_schemas = get_schemas_by_topic_name(topic_name)
    for enabled_schema in enabled_schemas:
        schema_json = simplejson.loads(enabled_schema.avro_schema)
        if not is_full_compatible(schema_json, target_schema):
            return False
    return True


def _construct_topic_name(namespace, source):
    return '.'.join((namespace, source, uuid.uuid4().hex))


def _create_topic(topic_name, domain):
    """Create a topic named `topic_name` in the domain of given namespace
    and source. It returns newly created topic. If a topic with same name
    already exists, an exception is thrown.
    """
    topic = models.Topic(name=topic_name, domain_id=domain.id)
    session.add(topic)
    session.flush()
    return topic


def get_topic_by_name(topic_name):
    """Get topic of specified topic name. It returns None if the specified
    topic is not found.
    """
    return session.query(
        models.Topic
    ).filter(
        models.Topic.name == topic_name
    ).first()


def get_domain_by_fullname(namespace, source):
    """Get the domain object of specified namespace and source. It returns
    None if no such domain exists.
    """
    return session.query(
        models.Domain
    ).filter(
        models.Domain.namespace == namespace,
        models.Domain.source == source
    ).first()


def _create_avro_schema(
        avro_schema_json,
        topic_id,
        status=models.AvroSchemaStatus.READ_AND_WRITE,
        base_schema_id=None
):
    avro_schema_elements = models.AvroSchema.create_schema_elements_from_json(
        avro_schema_json
    )

    required_doc_element_types = ['record', 'field']

    if any(o for o in avro_schema_elements
           if o.element_type in required_doc_element_types and not o.doc):
        raise MissingDocException(
            "Avro type {0} must provide `doc` value.".format(
                ', '.join(required_doc_element_types)
            )
        )

    avro_schema = models.AvroSchema(
        avro_schema_json=avro_schema_json,
        topic_id=topic_id,
        status=status,
        base_schema_id=base_schema_id
    )
    session.add(avro_schema)
    session.flush()

    for avro_schema_element in avro_schema_elements:
        avro_schema_element.avro_schema_id = avro_schema.id
        session.add(avro_schema_element)

    session.flush()
    return avro_schema


def get_schema_by_id(schema_id):
    """Get the Avro schema of specified id. It returns None if not found.
    """
    return session.query(
        models.AvroSchema
    ).filter(
        models.AvroSchema.id == schema_id
    ).first()


def get_latest_schema_by_topic_id(topic_id):
    """Get the latest enabled (Read-Write or Read-Only) schema of given topic.
    It returns None if no such schema can be found.
    """
    return session.query(
        models.AvroSchema
    ).filter(
        models.AvroSchema.topic_id == topic_id,
        models.AvroSchema.status != models.AvroSchemaStatus.DISABLED
    ).order_by(
        models.AvroSchema.id.desc()
    ).first()


def get_latest_schema_by_topic_name(topic_name):
    """Get the latest enabled (Read-Write or Read-Only) schema of given topic.
    It returns None if no such schema can be found.
    """
    topic = get_topic_by_name(topic_name)
    if not topic:
        raise EntityNotFoundException(
            "Cannot find topic {0}.".format(topic_name)
        )

    return session.query(
        models.AvroSchema
    ).filter(
        models.AvroSchema.topic_id == topic.id,
        models.AvroSchema.status != models.AvroSchemaStatus.DISABLED
    ).order_by(
        models.AvroSchema.id.desc()
    ).first()


def is_schema_compatible(target_schema, namespace, source):
    """Check whether given schema is a valid Avro schema. It then determines
    the topic of given Avro schema belongs to and checks the compatibility
    against the existing schemas in this topic. Note that given target_schema
    is expected as Avro json object.
    """
    topic = get_latest_topic_of_domain(namespace, source)
    if not topic:
        return True
    return is_schema_compatible_in_topic(target_schema, topic.name)


def get_schemas_by_topic_name(topic_name, include_disabled=False):
    topic = get_topic_by_name(topic_name)
    if not topic:
        raise EntityNotFoundException('{0} not found.'.format(topic_name))

    qry = session.query(
        models.AvroSchema
    ).filter(
        models.AvroSchema.topic_id == topic.id
    )
    if not include_disabled:
        qry = qry.filter(
            models.AvroSchema.status != models.AvroSchemaStatus.DISABLED
        )
    return qry.order_by(models.AvroSchema.id).all()


def get_schemas_by_topic_id(topic_id, include_disabled=False):
    """Get all the Avro schemas of specified topic. Default it excludes
    disabled schemas. Set `include_disabled` to True to include disabled ones.
    """
    qry = session.query(
        models.AvroSchema
    ).filter(
        models.AvroSchema.topic_id == topic_id
    )
    if not include_disabled:
        qry = qry.filter(
            models.AvroSchema.status != models.AvroSchemaStatus.DISABLED
        )
    return qry.order_by(models.AvroSchema.id).all()


def mark_schema_disabled(schema_id):
    """Disable the Avro schema of specified id.
    """
    _update_schema_status(schema_id, models.AvroSchemaStatus.DISABLED)


def mark_schema_readonly(schema_id):
    """Mark the Avro schema of specified id as read-only.
    """
    _update_schema_status(schema_id, models.AvroSchemaStatus.READ_ONLY)


def _update_schema_status(schema_id, status):
    session.query(
        models.AvroSchema
    ).filter(
        models.AvroSchema.id == schema_id
    ).update(
        {'status': status}
    )
    session.flush()


def get_domains():
    return session.query(models.Domain).order_by(models.Domain.id).all()


def get_namespaces():
    """Return a list of namespace strings"""
    result = session.query(models.Domain.namespace).distinct().all()
    return [namespace for (namespace,) in result]


def get_domains_by_namespace(namespace):
    return session.query(
        models.Domain
    ).filter(
        models.Domain.namespace == namespace
    ).order_by(
        models.Domain.id
    ).all()


def get_topics_by_domain_id(domain_id):
    return session.query(
        models.Topic
    ).filter(
        models.Topic.domain_id == domain_id
    ).order_by(
        models.Topic.id
    ).all()


def get_domain_by_id(domain_id):
    return session.query(
        models.Domain
    ).filter(
        models.Domain.id == domain_id
    ).first()


def get_latest_topic_of_domain_id(domain_id):
    """Get the latest topic of given domain_id. The latest one is the one
    created most recently. It returns None if no such topic exists.
    """
    return session.query(
        models.Topic
    ).filter(
        models.Topic.domain_id == domain_id
    ).order_by(
        models.Topic.id.desc()
    ).first()
