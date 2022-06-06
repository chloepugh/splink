import logging
from typing import TYPE_CHECKING

from .predict import predict_from_comparison_vectors_sqls
from .settings import Settings
from .m_u_records_to_parameters import m_u_records_to_lookup_dict
from .splink_dataframe import SplinkDataFrame
from .comparison_level import ComparisonLevel

# https://stackoverflow.com/questions/39740632/python-type-hinting-without-cyclic-imports
if TYPE_CHECKING:
    from .em_training_session import EMTrainingSession


logger = logging.getLogger(__name__)


def compute_new_parameters_sql(settings_obj: Settings):
    """compute m and u from results of predict"""

    sql_template = """
    select {gamma_column} as comparison_vector_value,

           sum(match_probability)/(select sum(match_probability)
            from __splink__df_predict where {gamma_column} != -1) as m_probability,

           sum(1 - match_probability)/(select sum(1 - match_probability)
            from __splink__df_predict where {gamma_column} != -1) as u_probability,

           '{output_column_name}' as output_column_name
    from __splink__df_predict
    where {gamma_column} != -1
    group by {gamma_column}
    """
    union_sqls = [
        sql_template.format(
            gamma_column=cc._gamma_column_name,
            output_column_name=cc._output_column_name,
        )
        for cc in settings_obj.comparisons
    ]

    # Proportion of matches
    sql = """
    select 0 as comparison_vector_value,
           avg(match_probability) as m_probability,
           avg(1-match_probability) as u_probability,
           '_proportion_of_matches' as output_column_name
    from __splink__df_predict
    """
    union_sqls.append(sql)

    sql = " union all ".join(union_sqls)

    return sql


def populate_m_u_from_lookup(
    em_training_session, comparison_level: ComparisonLevel, m_u_records_lookup
):
    cl = comparison_level
    c = comparison_level.comparison
    if not em_training_session._training_fix_m_probabilities:
        try:
            m_probability = m_u_records_lookup[c._output_column_name][
                cl._comparison_vector_value
            ]["m_probability"]

        except KeyError:
            m_probability = "level not observed in training dataset"
        cl.m_probability = m_probability

    if not em_training_session._training_fix_u_probabilities:
        try:
            u_probability = m_u_records_lookup[c._output_column_name][
                cl._comparison_vector_value
            ]["u_probability"]

        except KeyError:
            u_probability = "level not observed in training dataset"

        cl.u_probability = u_probability


def maximisation_step(em_training_session: "EMTrainingSession", param_records):

    settings_obj = em_training_session._settings_obj

    m_u_records = []
    for r in param_records:
        if r["output_column_name"] == "_proportion_of_matches":
            prop_record = r
        else:
            m_u_records.append(r)

    if not em_training_session._training_fix_proportion_of_matches:

        settings_obj._proportion_of_matches = prop_record["m_probability"]

    m_u_records_lookup = m_u_records_to_lookup_dict(m_u_records)
    for cc in settings_obj.comparisons:
        for cl in cc._comparison_levels_excluding_null:
            populate_m_u_from_lookup(em_training_session, cl, m_u_records_lookup)

    em_training_session._add_iteration()


def expectation_maximisation(
    em_training_session: "EMTrainingSession",
    df_comparison_vector_values: SplinkDataFrame,
):
    """In the expectation step, we use the current model parameters to estimate
    the probability of match for each pairwise record comparison

    In the maximisation step, we use these predicted probabilities to re-compute
    the parameters of the model
    """

    settings_obj = em_training_session._settings_obj
    linker = em_training_session._original_linker

    max_iterations = settings_obj._max_iterations
    em_convergece = settings_obj._em_convergence
    logger.info("")  # newline
    for i in range(1, max_iterations + 1):

        # Expectation step
        sqls = predict_from_comparison_vectors_sqls(settings_obj)
        for sql in sqls:
            linker._enqueue_sql(sql["sql"], sql["output_table_name"])

        sql = compute_new_parameters_sql(settings_obj)
        linker._enqueue_sql(sql, "__splink__df_new_params")
        df_params = linker._execute_sql_pipeline([df_comparison_vector_values])
        param_records = df_params.as_record_dict()

        df_params.drop_table_from_database()

        maximisation_step(em_training_session, param_records)
        max_change_dict = (
            em_training_session._max_change_in_parameters_comparison_levels()
        )
        logger.info(f"Iteration {i}: {max_change_dict['message']}")

        if max_change_dict["max_abs_change_value"] < em_convergece:
            break
    logger.info(f"\nEM converged after {i} iterations")
