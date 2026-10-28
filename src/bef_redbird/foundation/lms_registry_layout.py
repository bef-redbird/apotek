"""Typed native registry fields, retaining the source's natural record grains.

Ranges are one-based inclusive. Numeric attributes without documented decimal
scales remain lexical tokens. Retired/ambiguous fields are deliberately named as
tokens, not silently assigned the current specification's meaning.
"""

from __future__ import annotations

# name, first byte, last byte, representation (text, digits, ore, date)
Field = tuple[str, int, int, str]
LAYOUTS: dict[int, tuple[int, tuple[Field, ...]]] = {
    1: (
        180,
        (
            ("drug_id", 1, 11, "digits"),
            ("product_type", 12, 13, "text"),
            ("product_subtype", 14, 15, "text"),
            ("alphabetic_sequence", 16, 24, "digits"),
            ("speciality_number", 25, 29, "text"),
            ("name", 30, 59, "text"),
            ("form", 60, 79, "text"),
            ("form_code", 80, 86, "text"),
            ("additional_form_token", 87, 93, "text"),
            ("strength", 94, 113, "text"),
            ("strength_numeric_token", 114, 123, "text"),
            ("strength_unit", 124, 126, "text"),
            ("authorisation_holder", 127, 132, "text"),
            ("drug_distributor", 133, 138, "text"),
            ("atc", 139, 146, "text"),
            ("administration_codes", 147, 154, "text"),
            ("traffic_warning", 155, 155, "text"),
            ("substitution_flag", 156, 156, "text"),
            ("renewal_token", 157, 157, "text"),
            ("legacy_20", 158, 158, "text"),
            ("legacy_21", 159, 159, "text"),
            ("drug_substitution_group", 160, 163, "text"),
            ("dose_suitable", 164, 164, "text"),
            ("deregistration_date", 165, 172, "date"),
            ("quarantine_date", 173, 180, "date"),
        ),
    ),
    2: (
        137,
        (
            ("drug_id", 1, 11, "digits"),
            ("package_number", 12, 17, "digits"),
            ("alphabetic_sequence", 18, 20, "text"),
            ("component_package", 21, 26, "text"),
            ("component_count_token", 27, 29, "text"),
            ("pack_description", 30, 59, "text"),
            ("pack_size_token", 60, 67, "text"),
            ("pack_unit", 68, 69, "text"),
            ("container_code", 70, 73, "text"),
            ("dispensing_code", 74, 78, "text"),
            ("speciality_code", 79, 83, "text"),
            ("reimbursement_code", 84, 85, "text"),
            ("reimbursement_clause", 86, 90, "text"),
            ("ddd_token", 91, 99, "text"),
            ("storage_time_token", 100, 101, "text"),
            ("storage_time_unit", 102, 102, "text"),
            ("storage_code", 103, 103, "text"),
            ("creation_date", 104, 111, "date"),
            ("last_price_change_date", 112, 119, "date"),
            ("exit_date", 120, 127, "date"),
            ("calculation_code", 128, 128, "text"),
            ("reimbursement_group_flag", 129, 129, "text"),
            ("preparation_fee_flag", 130, 130, "text"),
            ("safety_features_token", 131, 131, "text"),
            ("package_distributor", 132, 137, "text"),
        ),
    ),
    3: (
        96,
        (
            ("package_number", 1, 6, "digits"),
            ("aip_ore", 7, 15, "ore"),
            ("registered_ore", 16, 24, "ore"),
            ("consumer_ore", 25, 33, "ore"),
            ("legacy_05", 34, 42, "text"),
            ("legacy_06", 43, 51, "text"),
            ("reimbursement_ore", 52, 60, "ore"),
            ("hospital_slot_08", 61, 69, "ore"),
            ("legacy_09", 70, 78, "text"),
            ("non_reimbursable_ore", 79, 87, "ore"),
            ("legacy_11", 88, 96, "text"),
        ),
    ),
    4: (
        47,
        (
            ("group_code", 1, 4, "text"),
            ("package_number", 5, 10, "digits"),
            ("pack_size_token", 11, 18, "text"),
            ("alphabetic_sequence", 19, 27, "text"),
            ("legacy_05", 28, 34, "text"),
            ("abc_code", 35, 35, "text"),
            ("cheapest_package", 36, 41, "text"),
            ("legacy_08", 42, 47, "text"),
        ),
    ),
    5: (10, (("group_code", 1, 4, "text"), ("package_number", 5, 10, "digits"))),
    7: (
        11,
        (
            ("group_code", 1, 4, "text"),
            ("package_number", 5, 10, "digits"),
            ("legacy_03", 11, 11, "text"),
        ),
    ),
    9: (
        60,
        (
            ("company_code", 1, 6, "text"),
            ("short_name", 7, 26, "text"),
            ("company_name", 27, 58, "text"),
            ("parallel_import_code", 59, 60, "text"),
        ),
    ),
    10: (
        69,
        (
            ("drug_id", 1, 11, "digits"),
            ("change_date", 12, 19, "date"),
            ("previous_name", 20, 69, "text"),
        ),
    ),
    11: (
        63,
        (
            ("code", 1, 2, "text"),
            ("short_text", 3, 12, "text"),
            ("description", 13, 62, "text"),
            ("legacy_04", 63, 63, "text"),
        ),
    ),
    12: (
        152,
        (("atc", 1, 8, "text"), ("description", 9, 80, "text"), ("legacy_07", 81, 152, "text")),
    ),
    13: (
        52,
        (("code", 1, 1, "text"), ("description", 2, 51, "text"), ("legacy_03", 52, 52, "text")),
    ),
    14: (
        65,
        (
            ("code", 1, 4, "text"),
            ("short_text", 5, 14, "text"),
            ("description", 15, 64, "text"),
            ("legacy_04", 65, 65, "text"),
        ),
    ),
    15: (
        65,
        (
            ("unit_type", 1, 1, "text"),
            ("code", 2, 4, "text"),
            ("short_text", 5, 14, "text"),
            ("description", 15, 64, "text"),
            ("legacy_05", 65, 65, "text"),
        ),
    ),
    16: (
        63,
        (
            ("code", 1, 2, "text"),
            ("short_text", 3, 12, "text"),
            ("description", 13, 62, "text"),
            ("legacy_04", 63, 63, "text"),
        ),
    ),
    17: (
        555,
        (("code", 1, 5, "text"), ("short_text", 6, 55, "text"), ("description", 56, 555, "text")),
    ),
    18: (
        67,
        (
            ("code", 1, 5, "text"),
            ("dispensing_group", 6, 6, "text"),
            ("short_text", 7, 16, "text"),
            ("description", 17, 66, "text"),
            ("legacy_05", 67, 67, "text"),
        ),
    ),
    19: (
        65,
        (("code", 1, 5, "text"), ("short_text", 6, 15, "text"), ("description", 16, 65, "text")),
    ),
    20: (
        61,
        (("code", 1, 1, "text"), ("short_text", 2, 11, "text"), ("description", 12, 61, "text")),
    ),
    21: (71, (("drug_id", 1, 11, "digits"), ("full_name", 12, 71, "text"))),
    22: (
        108,
        (
            ("code", 1, 7, "text"),
            ("description", 8, 107, "text"),
            ("active_code", 108, 108, "text"),
        ),
    ),
    23: (
        24,
        (
            ("patient_type", 1, 2, "text"),
            ("level_code", 3, 3, "text"),
            ("lower_ore", 4, 11, "ore"),
            ("upper_ore", 12, 19, "ore"),
            ("percent_token", 20, 24, "text"),
        ),
    ),
    24: (
        60,
        (
            ("drug_id", 1, 11, "digits"),
            ("package_number", 12, 17, "digits"),
            ("drug_group", 18, 21, "text"),
            ("minimum_aip_unit_ore", 22, 30, "ore"),
            ("minimum_registered_unit_ore", 31, 39, "ore"),
            ("reimbursement_unit_ore", 40, 48, "ore"),
            ("abc_code", 49, 49, "text"),
            ("cheapest_drug", 50, 60, "text"),
        ),
    ),
    25: (
        26,
        (("atc", 1, 8, "text"), ("indication_code", 9, 15, "text"), ("drug_id", 16, 26, "digits")),
    ),
    26: (
        164,
        (
            ("code", 1, 7, "text"),
            ("description", 8, 85, "text"),
            ("line_1", 86, 111, "text"),
            ("line_2", 112, 137, "text"),
            ("line_3", 138, 163, "text"),
            ("active_code", 164, 164, "text"),
        ),
    ),
    27: (18, (("drug_id", 1, 11, "digits"), ("dosage_code", 12, 18, "text"))),
    28: (
        183,
        (
            ("code", 1, 7, "text"),
            ("short_text", 8, 17, "text"),
            ("description", 18, 95, "text"),
            ("units_per_day_token", 96, 104, "text"),
            ("line_1", 105, 130, "text"),
            ("line_2", 131, 156, "text"),
            ("line_3", 157, 182, "text"),
            ("active_code", 183, 183, "text"),
        ),
    ),
    29: (
        46,
        (
            ("recommendation_group", 1, 4, "text"),
            ("drug_id", 5, 15, "digits"),
            ("package_number", 16, 21, "digits"),
            ("recommendation", 22, 46, "text"),
        ),
    ),
    30: (
        367,
        (
            ("drug_id", 1, 11, "digits"),
            ("package_number", 12, 17, "digits"),
            ("substance_class", 18, 117, "text"),
            ("substance_group", 118, 217, "text"),
            ("substance", 218, 367, "text"),
        ),
    ),
    31: (
        36,
        (
            ("drug_id", 1, 11, "digits"),
            ("package_number", 12, 17, "digits"),
            ("consumer_unit_token", 18, 26, "text"),
            ("consumer_ddd_token", 27, 35, "text"),
            ("cheapest_flag", 36, 36, "text"),
        ),
    ),
    32: (
        30,
        (
            ("ordered_package", 1, 6, "digits"),
            ("substitute_package", 7, 12, "digits"),
            ("alternative_package", 13, 18, "digits"),
            ("package_count_token", 19, 20, "text"),
            ("combination_consumer_token", 21, 29, "text"),
            ("information_flag", 30, 30, "text"),
        ),
    ),
    34: (
        21,
        (
            ("drug_id", 1, 11, "digits"),
            ("package_number", 12, 17, "digits"),
            ("patented_flag", 18, 18, "text"),
            ("indication_group", 19, 21, "text"),
        ),
    ),
    35: (2003, (("indication_group", 1, 3, "text"), ("description", 4, 2003, "text"))),
}
# Preliminary records have separate semantics; placeholder amounts stay lexical.
LAYOUTS[33] = (
    30,
    tuple(
        ("reserved_price_token" if n == "combination_consumer_token" else n, a, b, k)
        for n, a, b, k in LAYOUTS[32][1]
    ),
)
LAYOUTS[38] = (
    60,
    tuple(
        ("reserved_" + n if k == "ore" else n, a, b, "text" if k == "ore" else k)
        for n, a, b, k in LAYOUTS[24][1]
    ),
)

PROFILE = "lms-native-registry-v1"
