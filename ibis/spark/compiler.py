'''
Adding and subtracting timestamp/date intervals (dealt with in `_timestamp_op`)
is still WIP since Spark SQL support for these tasks is not comprehensive.
The built-in Spark SQL functions `date_add`, `date_sub`, and `add_months` do
not support timestamps, as they set the HH:MM:SS part to zero. The other option
is arithmetic syntax: <timestamp> + INTERVAL <num> <unit>, where unit is
something like MONTHS or DAYS. However, with the arithmetic syntax, <num>
must be a scalar, i.e. can't be a column like t.int_col.

                        supports        supports        preserves
                        scalars         columns         HH:MM:SS
                     _______________________________ _______________
built-in functions  |               |               |               |
like `date_add`     |      YES      |      YES      |       NO      |
                    |_______________|_______________|_______________|
                    |               |               |               |
arithmetic          |      YES      |       NO      |      YES      |
                    |_______________|_______________|_______________|

'''


import itertools
import math

import ibis
import ibis.common as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
import ibis.expr.types as ir
import ibis.sql.compiler as comp
import ibis.util as util
from ibis.impala import compiler as impala_compiler
from ibis.impala.compiler import (
    ImpalaContext,
    ImpalaDialect,
    ImpalaExprTranslator,
    ImpalaSelect,
    _reduction,
    fixed_arity,
    unary,
)


class SparkSelectBuilder(comp.SelectBuilder):
    @property
    def _select_class(self):
        return SparkSelect


class SparkQueryBuilder(comp.QueryBuilder):
    select_builder = SparkSelectBuilder


def build_ast(expr, context):
    builder = SparkQueryBuilder(expr, context=context)
    return builder.get_result()


class SparkContext(ImpalaContext):
    pass


_sql_type_names = impala_compiler._sql_type_names.copy()

_sql_type_names.update({
    'date': 'date',
})


def _cast(translator, expr):
    op = expr.op()
    arg, target_type = op.args
    arg_formatted = translator.translate(arg)

    if isinstance(arg, ir.CategoryValue) and target_type == dt.int32:
        return arg_formatted
    if isinstance(arg, ir.TemporalValue) and target_type == dt.int64:
        return '1000000 * unix_timestamp({})'.format(arg_formatted)
    else:
        sql_type = _type_to_sql_string(target_type)
        return 'CAST({} AS {})'.format(arg_formatted, sql_type)


def _type_to_sql_string(tval):
    if isinstance(tval, dt.Decimal):
        return 'decimal({}, {})'.format(tval.precision, tval.scale)
    name = tval.name.lower()
    try:
        return _sql_type_names[name]
    except KeyError:
        raise com.UnsupportedBackendType(name)


_spark_date_unit_names = {
    'Y': 'YEAR',
    'Q': 'QUARTER',
}

_spark_timestamp_unit_names = _spark_date_unit_names.copy()
_spark_timestamp_unit_names.update({
    'M': 'MONTH',
    'W': 'WEEK',
    'D': 'DAY',
    'h': 'HOUR',
    'm': 'MINUTE',
    's': 'SECOND'
})


def _timestamp_truncate(translator, expr):
    op = expr.op()
    arg, unit = op.args

    arg_formatted = translator.translate(arg)
    try:
        unit = _spark_timestamp_unit_names[unit]
    except KeyError:
        raise com.UnsupportedOperationError(
            '{!r} unit is not supported in timestamp truncate'.format(unit)
        )

    if unit == 'DAY':
        return "date(date_trunc({!r}, {}))".format(unit, arg_formatted)
    else:
        return "date_trunc({!r}, {})".format(unit, arg_formatted)


def _date_truncate(translator, expr):
    op = expr.op()
    arg, unit = op.args

    arg_formatted = translator.translate(arg)
    try:
        unit = _spark_date_unit_names[unit]
    except KeyError:
        raise com.UnsupportedOperationError(
            '{!r} unit is not supported in date truncate'.format(unit)
        )

    return "trunc({}, {!r})".format(arg_formatted, unit)


def _timestamp_from_unix(translator, expr):
    op = expr.op()

    val, unit = op.args
    val = util.convert_unit(val, unit, 's', floor=False)

    arg = translator.translate(val)
    return 'to_timestamp({})'.format(arg)


def _string_concat(translator, expr):
    return 'CONCAT({})'.format(
        ', '.join(map(translator.translate, expr.op().arg))
    )


def _group_concat(translator, expr):
    arg, sep = expr.op().arg, expr.op().sep
    return 'concat_ws({}, collect_list({}))'.format(
        translator.translate(sep),
        translator.translate(arg)
    )


def _array_literal_format(translator, expr):
    translated_values = [
        translator.translate(ibis.literal(x))
        for x in expr.op().value
    ]

    return 'array({})'.format(
        ', '.join(translated_values)
    )


def _struct_like_format(func):
    def formatter(translator, expr):
        translated_values = [
            (repr(name), translator.translate(ibis.literal(val)))
            for (name, val) in expr.op().value.items()
        ]

        return '{}({})'.format(
            func,
            ', '.join(itertools.chain(*translated_values))
        )
    return formatter


_literal_formatters = impala_compiler._literal_formatters.copy()

_literal_formatters.update({
    'array': _array_literal_format,
    'struct': _struct_like_format('named_struct'),
    'map': _struct_like_format('map'),
})


def _literal(translator, expr):
    if isinstance(expr, ir.BooleanValue):
        typeclass = 'boolean'
    elif isinstance(expr, ir.StringValue):
        typeclass = 'string'
    elif isinstance(expr, ir.NumericValue):
        typeclass = 'number'
    elif isinstance(expr, ir.DateValue):
        typeclass = 'date'
    elif isinstance(expr, ir.TimestampValue):
        typeclass = 'timestamp'
    elif isinstance(expr, ir.IntervalValue):
        typeclass = 'interval'
    elif isinstance(expr, ir.SetValue):
        typeclass = 'set'
    elif isinstance(expr, ir.ArrayValue):
        typeclass = 'array'
    elif isinstance(expr, ir.StructScalar):
        typeclass = 'struct'
    elif isinstance(expr, ir.MapScalar):
        typeclass = 'map'
    else:
        raise NotImplementedError(type(expr).__name__)

    return _literal_formatters[typeclass](translator, expr)


def _struct_field(translator, expr):
    arg, field = expr.op().args
    arg_formatted = translator.translate(arg)
    return '{}.`{}`'.format(arg_formatted, field)


def _map_value_for_key(translator, expr):
    arg, field = expr.op().args
    arg_formatted = translator.translate(arg)
    field_formatted = translator.translate(ibis.literal(field))
    return '{}[{}]'.format(arg_formatted, field_formatted)


def _round(translator, expr):
    op = expr.op()
    arg, digits = op.args

    arg_formatted = translator.translate(arg)

    if digits is not None:
        digits_formatted = translator.translate(digits)
        return 'bround({}, {})'.format(arg_formatted, digits_formatted)
    return 'bround({})'.format(arg_formatted)


_operation_registry = impala_compiler._operation_registry.copy()
_operation_registry.update(
    {
        ops.IsNan: unary('isnan'),
        ops.IfNull: fixed_arity('ifnull', 2),
        ops.StructField: _struct_field,
        ops.MapValueForKey: _map_value_for_key,
        ops.ArrayLength: unary('size'),
        ops.Round: _round,
        ops.HLLCardinality: _reduction('approx_count_distinct'),
        ops.StrRight: fixed_arity('right', 2),
        ops.StringSplit: fixed_arity('SPLIT', 2),
        ops.RegexSearch: fixed_arity('rlike', 2),
        ops.StringConcat: _string_concat,
        ops.ArrayConcat: fixed_arity('concat', 2),
        ops.GroupConcat: _group_concat,
        ops.Cast: _cast,
        ops.ExtractYear: unary('year'),
        ops.ExtractMonth: unary('month'),
        ops.ExtractDay: unary('day'),
        ops.ExtractHour: unary('hour'),
        ops.ExtractMinute: unary('minute'),
        ops.ExtractSecond: unary('second'),
        ops.TimestampTruncate: _timestamp_truncate,
        ops.TimestampFromUNIX: _timestamp_from_unix,
        ops.DateTruncate: _date_truncate,
        ops.Literal: _literal,
    }
)


class SparkExprTranslator(ImpalaExprTranslator):
    _registry = _operation_registry

    context_class = SparkContext


compiles = SparkExprTranslator.compiles
rewrites = SparkExprTranslator.rewrites


@compiles(ops.Arbitrary)
def spark_compiles_arbitrary(translator, expr):
    arg, how, where = expr.op().args

    if where is not None:
        arg = where.ifelse(arg, ibis.NA)

    if how in (None, 'first'):
        return 'first({}, True)'.format(translator.translate(arg))
    elif how == 'last':
        return 'last({}, True)'.format(translator.translate(arg))
    else:
        raise com.UnsupportedOperationError(
            '{!r} value not supported for arbitrary in Spark SQL'.format(how)
        )


@compiles(ops.DayOfWeekName)
def spark_compiles_day_of_week_name(translator, expr):
    arg = expr.op().arg
    return 'date_format({}, {!r})'.format(translator.translate(arg), 'EEEE')


@rewrites(ops.IsInf)
def spark_rewrites_is_inf(expr):
    arg = expr.op().arg
    return (arg == ibis.literal(math.inf)) | (arg == ibis.literal(-math.inf))


class SparkSelect(ImpalaSelect):
    translator = SparkExprTranslator


class SparkDialect(ImpalaDialect):
    translator = SparkExprTranslator


dialect = SparkDialect
