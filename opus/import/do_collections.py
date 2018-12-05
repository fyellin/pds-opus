################################################################################
# do_collections.py
#
# Create an empty collections table.
################################################################################

import os

from config_data import *
import impglobals
import import_util


def create_collections():
    # There's really no point in doing this as an import table first,
    # since we're just creating an empty table.
    db = impglobals.DATABASE
    if not db.table_exists('perm', 'obs_general'):
        # We can't create collections here, because it has a foreign key
        # constraint on obs_general. But we needed to have tried because we
        # need to be able to delete things from obs_general and have
        # collections be empty! Chicken and egg.
        # So what we do is check here to see if obs_general exists. If it does,
        # we can go ahead and remove and re-create collections. If it doesn't,
        # then we don't do anything right now but set a flag to say that we'll
        # try again at the end of the import.
        if impglobals.TRY_COLLECTIONS_LATER:
            # Oops! We've already been down this road once, and apparently the
            # creation of obs_general failed. So we can't do anything.
            impglobals.LOGGER.log('error',
    'Unable to create "collections" table because "obs_general" doesn\'t exist')
            return
        impglobals.TRY_COLLECTIONS_LATER = True
        impglobals.LOGGER.log('warning',
    'Unable to create "collections" table because "obs_general" doesn\'t exist'
    +' - Will try again later')
        return
    collections_schema = import_util.read_schema_for_table('collections')
    db.drop_table('perm', 'collections')
    db.create_table('perm', 'collections', collections_schema,
                    ignore_if_exists=False)
