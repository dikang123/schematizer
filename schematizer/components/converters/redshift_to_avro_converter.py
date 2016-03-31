# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

from yelp_avro.avro_builder import AvroSchemaBuilder
from yelp_avro.data_pipeline.avro_meta_data import AvroMetaDataKeys

from schematizer.components.converters.converter_base import BaseConverter
from schematizer.components.converters.converter_base \
    import SchemaConversionException
from schematizer.components.converters.converter_base \
    import UnsupportedTypeException
from schematizer.models import redshift_data_types as redshift_types
from schematizer.models import SchemaKindEnum
from schematizer.models.sql_entities import MetaDataKey
from schematizer.models.sql_entities import SQLTable


class RedshiftToAvroConverter(BaseConverter):
    """Converter that converts Redshift table schema to Avro schema.
    """

    source_type = SchemaKindEnum.Redshift
    target_type = SchemaKindEnum.Avro

    def __init__(self):
        self._builder = AvroSchemaBuilder()

    def convert(self, src_schema):
        """The src_schema is the SQLTable object
        that represents a Redshift table.
        It returns the Avro schema json object.
        """
        if not src_schema:
            return None
        if not isinstance(src_schema, SQLTable):
            raise SchemaConversionException('SQLTable is expected.')

        return self._create_avro_record_json(src_schema)

    def _create_avro_record_json(self, table):
        namespace = table.metadata.get(MetaDataKey.NAMESPACE, '')
        aliases = table.metadata.get(MetaDataKey.ALIASES)
        metadata = self._get_table_metadata(table)

        self._builder.begin_record(
            table.name,
            namespace=namespace,
            aliases=aliases,
            doc=table.doc,
            **metadata
        )
        for column in table.columns:
            self._create_avro_field(column)
        record_json = self._builder.end()
        return record_json

    def _get_table_metadata(self, table):
        metadata = {}

        primary_keys = [c.name for c in table.primary_keys]
        if primary_keys:
            metadata[AvroMetaDataKeys.PRIMARY_KEY] = primary_keys

        return metadata

    def _create_avro_field(self, column):
        field_type, field_metadata = self._create_avro_field_type(column)
        if column.is_nullable:
            field_type = self._builder.begin_nullable_type(
                field_type,
                column.default_value
            ).end()

        self._builder.add_field(
            column.name,
            field_type,
            has_default=column.default_value is not None or column.is_nullable,
            default_value=column.default_value,
            aliases=column.metadata.get(MetaDataKey.ALIASES),
            doc=column.doc,
            **field_metadata
        )

    def _create_avro_field_type(self, column):
        type_cls = column.type.__class__
        convert_func = self._type_converters.get(type_cls)
        if convert_func:
            return convert_func(column)

        raise UnsupportedTypeException(
            "Unable to convert redshift data type {} to Avro schema type."
            .format(column.type)
        )

    @property
    def _type_converters(self):
        return {
            redshift_types.RedshiftFloat4: self._convert_float_type,
            redshift_types.RedshiftReal: self._convert_float_type,

            redshift_types.RedshiftFloat: self._convert_double_type,
            redshift_types.RedshiftDouble: self._convert_double_type,
            redshift_types.RedshiftFloat8: self._convert_double_type,

            redshift_types.RedshiftInt2: self._convert_small_integer_type,
            redshift_types.RedshiftInt4: self._convert_small_integer_type,
            redshift_types.RedshiftSmallInt: self._convert_small_integer_type,
            redshift_types.RedshiftInteger: self._convert_small_integer_type,

            redshift_types.RedshiftInt8: self._convert_bigint_type,
            redshift_types.RedshiftBigInt: self._convert_bigint_type,

            redshift_types.RedshiftNumeric: self._convert_decimal_type,
            redshift_types.RedshiftDecimal: self._convert_decimal_type,

            redshift_types.RedshiftBool: self._convert_boolean_type,
            redshift_types.RedshiftBoolean: self._convert_boolean_type,

            redshift_types.RedshiftNChar: self._convert_char_type,
            redshift_types.RedshiftBPChar: self._convert_char_type,
            redshift_types.RedshiftChar: self._convert_char_type,
            redshift_types.RedshiftCharacter: self._convert_char_type,

            redshift_types.RedshiftNVarChar: self._convert_varchar_type,
            redshift_types.RedshiftCharacterVarying: \
                self._convert_varchar_type,
            redshift_types.RedshiftVarChar: self._convert_varchar_type,
            redshift_types.RedshiftText: self._convert_varchar_type,

            redshift_types.RedshiftDate: self._convert_date_type,
            redshift_types.RedshiftTimestamp: self._convert_timestamp_type,
        }

    def _convert_small_integer_type(self, column):
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        return self._builder.create_int(), metadata

    def _get_primary_key_metadata(self, primary_key_order):
        return ({AvroMetaDataKeys.PRIMARY_KEY: primary_key_order}
                if primary_key_order else {})

    def _convert_bigint_type(self, column):
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        return self._builder.create_long(), metadata

    def _convert_boolean_type(self, column):
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        return self._builder.create_boolean(), metadata

    def _convert_double_type(self, column):
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        return self._builder.create_double(), metadata

    def _get_precision_metadata(self, column):
        return {
            AvroMetaDataKeys.PRECISION: column.type.precision,
            AvroMetaDataKeys.SCALE: column.type.scale,
        }

    def _convert_float_type(self, column):
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        return self._builder.create_float(), metadata

    def _convert_decimal_type(self, column):
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        metadata.update({AvroMetaDataKeys.FIXED_POINT: True})
        metadata.update(self._get_precision_metadata(column))
        return self._builder.create_double(), metadata

    def _convert_char_type(self, column):
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        metadata[AvroMetaDataKeys.FIX_LEN] = column.type.length
        return self._builder.create_string(), metadata

    def _convert_varchar_type(self, column):
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        metadata[AvroMetaDataKeys.MAX_LEN] = column.type.length
        return self._builder.create_string(), metadata

    def _convert_date_type(self, column):
        """Avro currently doesn't support date, so map the
        date sql column type to string (ISO 8601 format)
        """
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        metadata[AvroMetaDataKeys.DATE] = True
        return self._builder.create_int(), metadata

    def _convert_time_type(self, column):
        """Avro currently doesn't support time, so map the
        time sql column type to string (ISO 8601 format)
        """
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        metadata[AvroMetaDataKeys.TIME] = True
        return self._builder.create_int(), metadata

    def _convert_timestamp_type(self, column):
        """Avro currently doesn't support timestamp, so map the
        timestamp sql column type to long (unix timestamp)
        """
        metadata = self._get_primary_key_metadata(column.primary_key_order)
        metadata[AvroMetaDataKeys.TIMESTAMP] = True
        return self._builder.create_long(), metadata
