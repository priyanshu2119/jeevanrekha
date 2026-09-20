"""English language pack -- the source of truth for all keys.

Wording rules followed here: short sentences, plain words, calm and
directive tone, no clinical jargon, no hedging, no false certainty. Status
messages state exactly what the system knows and nothing more.
"""

STRINGS: dict[str, str] = {
    # --- call opening -------------------------------------------------------
    "ivr_welcome": (
        "Welcome to JeevanRekha. When something looks wrong in pregnancy or "
        "with a newborn baby, we help you reach the right care fast. This "
        "service is not a doctor and does not tell you what illness someone has."
    ),
    "ivr_lang_prompt": (
        "Choose your language. For Hindi press 1. For English press 2. "
        "For Marathi press 3. For Bengali press 4. For Tamil press 5. "
        "For Telugu press 6."
    ),
    "ivr_track_prompt": (
        "Is this about a pregnant woman, or about a baby? "
        "For a pregnant woman press 1. For a baby press 2."
    ),
    "ivr_track_maternal": "About a pregnant woman",
    "ivr_track_newborn": "About a baby",
    "ivr_q_prefix": "Question {n} of {total}.",
    "ivr_answer_prompt": "If yes, press 1. If no, press 2. If you cannot say, press 3.",
    "ivr_yes": "Yes",
    "ivr_no": "No",
    "ivr_dontknow": "Cannot say",
    "ivr_invalid": "I did not understand. If yes press 1. If no press 2. If you cannot say press 3.",
    "ivr_silence": "Are you there? If yes press 1. If no press 2. If you cannot say press 3.",
    "ivr_unclear_ack": (
        "That is all right. Not being sure is important information too. "
        "For your safety, we are treating this as a warning sign."
    ),
    "ivr_region_prompt": (
        "To connect you to local help, tell us your area. "
        "Enter your area code now, then press the hash key."
    ),
    "ivr_region_unknown": (
        "We could not match that area code. We will continue, and route your "
        "call to the district control room instead."
    ),

    # --- results --------------------------------------------------------------
    "ivr_result_emergency": (
        "This looks like an emergency. Please do not panic. We are already "
        "getting help moving on two routes at the same time. Stay on the line "
        "if you can, and follow the instructions you hear now."
    ),
    "ivr_result_urgent": (
        "This needs a health worker's check soon -- today, or within 24 hours. "
        "It is not an emergency this minute, but please do not ignore it."
    ),
    "ivr_result_reassurance": (
        "From your answers, this does not look like an emergency. We will now "
        "share some simple care advice. If anything worries you later, call "
        "again -- you will never be a bother to us."
    ),
    "ivr_ref": "Your reference number is {ref}. Tell this number to the health worker.",
    "ivr_goodbye": "Thank you for calling JeevanRekha. Take care.",

    "tier_emergency": "Emergency -- help is being arranged now",
    "tier_urgent": "See a health worker within 24 hours",
    "tier_reassurance": "Likely fine -- with care advice",

    # --- honest live dispatch status -------------------------------------------
    "st_amb_requested": (
        "Emergency transport has been requested from {number}. We have NOT yet "
        "received confirmation that it is on the way. We will tell you the "
        "moment anything changes."
    ),
    "st_backup_requested": (
        "At the same time we are alerting your local backup: {name} ({kind}). "
        "Two routes are being tried, not one."
    ),
    "st_waiting": "Still waiting for confirmation. We have not stopped trying.",
    "st_amb_confirmed": (
        "Ambulance dispatch CONFIRMED at {time}. Keep this phone nearby. If it "
        "is safe, keep the main door unlocked and send someone to the road "
        "head to guide the vehicle."
    ),
    "st_backup_confirmed": "{name} has confirmed and is moving to help.",
    "st_timeout": (
        "No confirmation yet from {name}. We are now contacting the next "
        "person in the chain: {next_name}. This happens automatically -- we "
        "do not go silent."
    ),
    "st_amb_timeout_retry": (
        "No confirmation from the ambulance service yet. We are requesting "
        "again (attempt {attempt})."
    ),
    "st_exhausted": (
        "Every contact in the chain has been tried. A JeevanRekha operator has "
        "been alerted and is working on this personally right now. If you can "
        "arrange any vehicle, please leave for the nearest hospital "
        "immediately -- do not wait only for us."
    ),
    "st_operator_alerted": "An operator has been alerted and is following up by hand.",
    "st_cancelled": "Dispatch cancelled.",
    "st_both_confirmed": "Both routes have confirmed. Help is on the way.",
    "triage_decided": "Triage decision recorded: {tier}.",
    "caller_disconnected_emergency_active": (
        "The caller disconnected. Emergency routing is still active -- "
        "responders are still being contacted."
    ),

    # --- maternal questions ------------------------------------------------------
    "q_m_bleeding": "Is she bleeding heavily from below?",
    "q_m_fits": "Has she had fits or convulsions, or become unconscious?",
    "q_m_breathing": "Is she having severe difficulty in breathing?",
    "q_m_fever": "Does she have a high fever?",
    "q_m_headache": "Does she have a severe headache, or is her vision blurred?",
    "q_m_pain": "Does she have severe pain in the belly?",
    "q_m_movement": "Has the baby's movement inside reduced or stopped?",
    "h_m_movement": "If she is five months pregnant or more.",
    "q_m_leaking": "Has water started leaking from below before the baby is due?",
    "h_m_leaking": "Watery leaking -- not urine.",
    "q_m_swelling": "Is there swelling on her face, her hands or her feet?",
    "q_m_vomiting": "Is she vomiting so much that even water does not stay inside?",

    # --- newborn questions -------------------------------------------------------
    "q_n_feeding": "Is the baby not feeding at all, or feeding much less than usual?",
    "q_n_fits": "Has the baby had fits or convulsions?",
    "q_n_breathing": "Is the baby breathing very fast, or is the chest pulling in?",
    "q_n_temp_hot": "Does the baby's body feel very hot?",
    "q_n_temp_cold": "Does the baby's body feel cold?",
    "q_n_sleepy": "Is the baby unusually sleepy, or not waking up to feed?",
    "q_n_jaundice": "Are the baby's palms or soles looking yellow?",
    "h_n_jaundice": "Also if the whites of the eyes look deep yellow.",
    "q_n_cord": "Is there redness or pus around the baby's navel?",
    "q_n_stools": "Is the baby passing many watery stools, or vomiting everything?",

    # --- emergency first-response guidance (public health-education level) --------
    "g_stay_on_line": (
        "Stay on the line if you can. Help has been requested on two routes at "
        "the same time."
    ),
    "g_bleeding": (
        "Make her lie down on her left side. Cover her with a blanket or cloth "
        "to keep her warm. Count how many pads or cloths got soaked and show "
        "them to the health worker."
    ),
    "g_fits": (
        "If she has fits: lay her on her side, put something soft under her "
        "head, put nothing inside her mouth, and do not hold her down by force."
    ),
    "g_breathing": (
        "Help her sit upright. Loosen tight clothing around the chest and "
        "neck. Keep fresh air moving in the room."
    ),
    "g_fever": (
        "Wipe her body with a cloth dipped in ordinary room-temperature water. "
        "Keep her clothing light."
    ),
    "g_headache": (
        "Keep her lying on her left side in a quiet, dim place. Do not delay "
        "going to the hospital."
    ),
    "g_pain": "Let her lie in whatever position is most comfortable. Keep her warm.",
    "g_movement": (
        "Have her lie on her left side and check once more for the baby's "
        "movement. If movement is still reduced, go to the hospital now."
    ),
    "g_leaking": (
        "Use a clean cloth or pad. Note the time the leaking started and the "
        "colour of the water. Keep her lying down."
    ),
    "g_no_food_drink": (
        "Do not give her anything to eat or drink right now -- in case the "
        "hospital needs to operate quickly."
    ),
    "g_ready_transport": (
        "Keep ready: her health card, some money, a phone charger, and a clean "
        "cloth or blanket for the baby."
    ),
    "g_not_feeding": (
        "Keep the baby warm against the mother's bare chest, skin to skin. If "
        "the mother can express milk, feed it with a clean spoon. Do not force "
        "the baby."
    ),
    "g_fast_breathing": (
        "Hold the baby upright against your chest. Loosen the baby's clothing. "
        "Keep smoke and dust away from the baby."
    ),
    "g_hot_baby": (
        "Remove extra clothing and blankets. Wipe the baby with a cloth of "
        "ordinary water -- never cold or ice water."
    ),
    "g_cold_baby": (
        "Warm the baby now: skin to skin against the mother's chest, cover "
        "both with a blanket, put a cap on the baby's head. A cold baby is an "
        "emergency."
    ),
    "g_sleepy_baby": (
        "Try to feed the baby. If the baby is too sleepy to feed, keep the "
        "baby warm skin to skin and go to the hospital now."
    ),
    "g_jaundice": (
        "Yellow palms and soles need an urgent hospital check. Keep feeding "
        "the baby and go now."
    ),
    "g_cord": (
        "Keep the navel clean and dry. Do not apply oil, paste or anything "
        "else on it."
    ),
    "g_stools": (
        "Keep feeding the baby -- breast milk is best. Watch for a dry mouth "
        "or sunken eyes; these mean the baby is losing water."
    ),
    "g_swelling": (
        "Swelling of the face, hands or feet in pregnancy can be a warning "
        "sign. Rest, take less salt, and get checked by a health worker within "
        "24 hours."
    ),
    "g_vomiting": (
        "Give small sips of water or ORS often. If even water does not stay "
        "inside, see a health worker today."
    ),

    # --- urgent tier -------------------------------------------------------------
    "u_contact_today": (
        "Contact your ASHA worker, the ANM, or the nearest health centre "
        "today -- within 24 hours. Do not wait for days."
    ),
    "u_watch_danger": (
        "Watch for danger signs: heavy bleeding, fits, high fever, difficulty "
        "in breathing, or the baby not feeding. If any of these appears, call "
        "us back immediately or go to the nearest hospital."
    ),
    "u_call_back": (
        "Calling again is free, and you will never be scolded for it. When in "
        "doubt, always call."
    ),

    # --- reassurance tier ----------------------------------------------------------
    "r_not_dismissive": (
        "We are glad you called. Checking early is exactly the right thing to "
        "do. Here is some simple care advice."
    ),
    "g_rest": (
        "Rest well. Sleep when the household rests, and in late pregnancy lie "
        "on your left side when sleeping."
    ),
    "g_meals": (
        "Eat two extra meals a day over your normal food -- during pregnancy "
        "and while breastfeeding."
    ),
    "g_iron": "Take the iron and folic acid tablets exactly as your health worker prescribed.",
    "g_fluids": "Drink plenty of safe water every day -- boiled or treated.",
    "g_anc": "Attend every antenatal checkup, even when you feel completely well.",
    "g_delivery_plan": (
        "Plan the delivery at a health facility. Keep ready: a bag, some "
        "money, transport numbers, and decide in advance who will accompany."
    ),
    "r_danger_awareness": (
        "Remember the danger signs: bleeding, fits, high fever, breathing "
        "difficulty, severe headache or blurred vision, severe belly pain, "
        "reduced baby movement, water leaking. If any of these appears, call "
        "immediately -- day or night."
    ),
    "g_breastfeed": (
        "Feed only breast milk for the first six months, with the first feed "
        "within one hour of birth. No water and no other milk unless a health "
        "worker advises it."
    ),
    "g_keep_warm": (
        "Keep the baby warm: skin to skin, a cap on the head, a warm room, no "
        "drafts. Bathe the baby only after the first few days, and only in a "
        "warm room."
    ),
    "g_cord_care": (
        "Keep the cord stump clean and dry. Apply nothing on it. It will fall "
        "off on its own."
    ),
    "g_vaccine": (
        "Complete every vaccination on the schedule given by the health "
        "worker. Write the dates down."
    ),
    "r_danger_awareness_baby": (
        "Remember the baby's danger signs: not feeding, fits, fast breathing "
        "or chest pulling in, body very hot or very cold, unusual sleepiness, "
        "yellow palms or soles. If any of these appears, call immediately -- "
        "day or night."
    ),

    # --- contact kinds -------------------------------------------------------------
    "kind_asha": "ASHA worker",
    "kind_phc": "Primary health centre",
    "kind_local_transport": "Local emergency transport",
    "kind_ambulance_node": "Ambulance node",
    "kind_district_control": "District control room",

    # --- web companion chrome --------------------------------------------------------
    "web_disclaimer": (
        "JeevanRekha is a triage and emergency-routing service. It is not a "
        "doctor, not a diagnostic tool, and gives no medical advice beyond "
        "helping you reach the right care fast."
    ),
    "web_start_maternal": "Something is wrong with a pregnant woman",
    "web_start_newborn": "Something is wrong with a baby",
    "btn_yes": "Yes",
    "btn_no": "No",
    "btn_unsure": "Cannot say",
    "btn_speak": "Read aloud",
    "btn_stop_speak": "Stop",
}
