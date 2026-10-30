"""The maintenance calendar: one shared Google calendar, read and written through
Henk's own scoped service account (maintenance-calendar spec).

``token`` holds the credential and the JWT-bearer sign-in, ``client`` the two
Calendar API calls and the one admission rule. Kept import-light on purpose:
``henk.config`` imports the credential parser from here.
"""
