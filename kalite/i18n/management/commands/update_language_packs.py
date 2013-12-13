"""
This command is the master command for language packs. Based on
command line arguments provided, it calls all i18n commands
necessary to update language packs.

1. Updates all cached srt files  from Amara
2. Downloads latest translations from CrowdIn
3. Generates metadata on language packs (subtitles and UI translations)
4. Compiles the UI translations
5. Zips up the packs and exposes them at a static url

Good test cases:

./manage.py -l aa # language with subtitles, no translations
./manage.py -l ur-PK # language with translations, no subtitles

NOTE: all language codes internally are assumed to be in django format (e.g. en_US)
"""
import datetime
import fnmatch
import glob
import json
import os
import re
import requests
import shutil
import subprocess
import sys
import tempfile
import zipfile
import StringIO
from optparse import make_option

from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command
from django.core.mail import mail_admins

import polib

import settings
import version
from settings import LOG as logging
from shared.i18n import get_language_pack_availability_filepath, LOCALE_ROOT, SUBTITLE_COUNTS_FILEPATH
from shared.i18n import get_language_name, lcode_to_django_dir, lcode_to_ietf, LanguageNotFoundError, get_language_pack_metadata_filepath, get_language_pack_filepath, move_old_subtitles, scrub_locale_paths
from update_po import compile_po_files
from utils.general import ensure_dir, version_diff


CROWDIN_CACHE_DIR = os.path.join(settings.PROJECT_PATH, "..", "_crowdin_cache")

class Command(BaseCommand):
    help = 'Updates all language packs'

    option_list = BaseCommand.option_list + (
        make_option('-d', '--days',
                    action='store',
                    dest='days',
                    default=1 if not settings.DEBUG else 365,
                    metavar="NUM_DAYS",
                    help="Update any and all subtitles that haven't been refreshed in the numebr of days given. Defaults to 0 days."),
        make_option('-l', '--lang_code',
                    action='store',
                    dest='lang_code',
                    default="all",
                    metavar="LANG_CODE",
                    help="Language code to update (default: all)"),
        make_option('--no-srts',
                    action='store_true',
                    dest='no_srts',
                    default=False,
                    help='Do not download and bundle video subtitles.'),
        make_option('--no_ka',
                    action='store_true',
                    dest='no_ka',
                    default=False,
                    help='Do not include Khan Academy content translations.'),
        make_option('--zip_file',
                    action='store',
                    dest='zip_file',
                    default=None,
                    help='a local zip file to be used instead of fetching to CrowdIn. Ignores -l if this is used.'),
        make_option('--ka_zip_file',
                    action='store',
                    dest='ka_zip_file',
                    default=None,
                    help='a local zip file to be used for KA content instead of fetching to CrowdIn. Ignores -l if this is used.'),
        make_option('-o', '--use_local',
                    action='store_true',
                    dest='use_local',
                    default=False,
                    metavar="USE_LOCAL",
                    help="Use the local po files, instead of refreshing from online (a way to test translation tweaks)"),
    )

    def handle(self, *args, **options):
        if not settings.CENTRAL_SERVER:
            raise CommandError("This must only be run on the central server.")
        if not options["lang_code"] or options["lang_code"].lower() == "all":
            lang_codes = ['all']
        else:
            lang_codes = [lcode_to_django_dir(lc) for lc in options["lang_code"].split(",")]

        upgrade_old_schema()

        # Raw language code for srts
        if not options['no_srts']:
            update_srts(days=options["days"], lang_codes=lang_codes)

        # Converted language code for language packs
        update_language_packs(
            lang_codes=lang_codes,
            zip_file=options['zip_file'],
            ka_zip_file=options['ka_zip_file'],
            download_ka_translations=not options['no_ka'],
            use_local=options["use_local"],
        )


def update_srts(days, lang_codes):
    """
    Run the commands to update subtitles that haven't been updated in the number of days provided.
    Default is to update all srt files that haven't been requested in 30 days
    """
    date = '{0.month}/{0.day}/{0.year}'.format(datetime.date.today() - datetime.timedelta(int(days)))
    logging.info("Updating subtitles that haven't been refreshed since %s" % date)
    call_command("generate_subtitle_map", date_since_attempt=date)
    if lang_codes:
        for lang_code in lang_codes:
            call_command("cache_subtitles", date_since_attempt=date, lang_code=lang_code)
    else:
        call_command("cache_subtitles", date_since_attempt=date)


def update_language_packs(lang_codes=None, download_ka_translations=True, zip_file=None, ka_zip_file=None, use_local=False):

    logging.info("Downloading %s language(s)" % lang_codes)

    package_metadata = {}

    if not use_local:
        # Download latest UI translations from CrowdIn
        assert hasattr(settings, "CROWDIN_PROJECT_ID") and hasattr(settings, "CROWDIN_PROJECT_KEY"), "Crowdin keys must be set to do this."


        # Download Khan Academy translations too
        if download_ka_translations:
            assert hasattr(settings, "KA_CROWDIN_PROJECT_ID") and hasattr(settings, "KA_CROWDIN_PROJECT_KEY"), "KA Crowdin keys must be set to do this."

        for lang_code in (lang_codes or [None]):
            lang_code = lcode_to_ietf(lang_code)

            package_metadata[lang_code] = {}

            logging.info("Downloading KA Lite translations...")
            kalite_po_file = download_latest_translations(
                lang_code=lang_code,
                project_id=settings.CROWDIN_PROJECT_ID,
                project_key=settings.CROWDIN_PROJECT_KEY,
                zip_file=zip_file or (os.path.join(CROWDIN_CACHE_DIR, "kalite-%s.zip" % lang_code) if settings.DEBUG else None),
            )
            kalite_metadata = get_po_metadata(kalite_po_file)
            package_metadata[lang_code]["ntranslations"] = kalite_metadata["ntranslations"]
            package_metadata[lang_code]["nphrases"]      = kalite_metadata["nphrases"]
            package_metadata[lang_code]["kalite_ntranslations"] = kalite_metadata["ntranslations"]
            package_metadata[lang_code]["kalite_nphrases"]      = kalite_metadata["nphrases"]

            # Download Khan Academy translations too
            if download_ka_translations:
                assert hasattr(settings, "KA_CROWDIN_PROJECT_ID") and hasattr(settings, "KA_CROWDIN_PROJECT_KEY"), "KA Crowdin keys must be set to do this."

                logging.info("Downloading Khan Academy translations...")
                combined_po_file = download_latest_translations(
                    lang_code=lang_code,
                    project_id=settings.KA_CROWDIN_PROJECT_ID,
                    project_key=settings.KA_CROWDIN_PROJECT_KEY,
                    zip_file=ka_zip_file or (os.path.join(CROWDIN_CACHE_DIR, "ka-%s.zip" % lang_code) if settings.DEBUG else None),
                    combine_with_po_file=kalite_po_file,
                    rebuild=False,  # just to be friendly to KA--we shouldn't force a rebuild
                    download_type="ka",
                )
                ka_metadata = get_po_metadata(combined_po_file)
                package_metadata[lang_code]["ntranslations"] = ka_metadata["ntranslations"]
                package_metadata[lang_code]["nphrases"]      = ka_metadata["nphrases"]
                package_metadata[lang_code]["ka_ntranslations"] = ka_metadata["ntranslations"] - package_metadata[lang_code]["kalite_ntranslations"]
                package_metadata[lang_code]["ka_nphrases"]      = ka_metadata["nphrases"] - package_metadata[lang_code]["kalite_nphrases"]

            # Now that we have metadata, compress by removing non-translated "translations"

    # Compile
    (out, err, rc) = compile_po_files(lang_codes=lang_codes)  # converts to django
    broken_langs = handle_po_compile_errors(lang_codes=lang_codes, out=out, err=err, rc=rc)

    # Loop through new UI translations & subtitles, create/update unified meta data
    logging.debug("Language metadata: %s" % package_metadata)
    generate_metadata(lang_codes=lang_codes, broken_langs=broken_langs, added_ka=download_ka_translations, package_metadata=package_metadata)

    # Zip
    package_sizes = zip_language_packs(lang_codes=lang_codes)
    logging.debug("Package sizes: %s" % package_sizes)

    # Loop through new UI translations & subtitles, create/update unified meta data
    update_metadata(sizes=package_sizes)


def upgrade_old_schema():
    """Move srt files from static/srt to locale directory and file them by language code, delete any old locale directories"""

    scrub_locale_paths()
    move_old_subtitles()

def handle_po_compile_errors(lang_codes=None, out=None, err=None, rc=None):
    """
    Return list of languages to not rezip due to errors in compile process.
    Then email admins errors.
    """

    broken_codes = re.findall(r'(?<=ka-lite/locale/)\w+(?=/LC_MESSAGES)', err) or []

    if lang_codes:
        # Only show the errors relevant to the list of language codes passed in.
        lang_codes = set([lcode_to_django_dir(lc) for lc in lang_codes])
        broken_codes = list(set(broken_codes).intersection(lang_codes))

    if broken_codes:
        logging.warning("Found %d errors while compiling in codes %s. Mailing admins report now."  % (len(broken_codes), ', '.join(broken_codes)))
        subject = "Error while compiling po files"
        commands = "\n".join(["python manage.py compilemessages -l %s" % lc for lc in broken_codes])
        message =  """The following codes had errors when compiling their po files: %s.
                   Please rerun the following commands to see specific line numbers
                   that need to be corrected on CrowdIn, before we can update the language packs.
                   %s""" % (
            ', '.join([lcode_to_ietf(lc) for lc in broken_codes]),
            commands,
        )
        if not settings.DEBUG:
            mail_admins(subject=subject, message=message)
            logging.info("Report sent.")
        else:
            logging.info("DEBUG is True so not sending email, but would have sent the following: SUBJECT: %s; MESSAGE: %s"  % (subject, message))

    return broken_codes


def download_latest_translations(project_id=settings.CROWDIN_PROJECT_ID,
                                 project_key=settings.CROWDIN_PROJECT_KEY,
                                 lang_code="all",
                                 zip_file=None,
                                 combine_with_po_file=None,
                                 rebuild=True,
                                 download_type=None):
    """
    Download latest translations from CrowdIn to corresponding locale
    directory. If zip_file is given, use that as the zip file
    instead of going through CrowdIn.

    """
    lang_code = lcode_to_ietf(lang_code)

    # Get zip file of translations
    if zip_file and os.path.exists(zip_file):
        logging.info("Using local zip file at %s" % zip_file)
        z = zipfile.ZipFile(zip_file)
        # use the name of the zip file to infer the language code, if needed
        lang_code = lang_code or os.path.splitext(os.path.basename(zip_file))[0]

    else:
        # Tell CrowdIn to Build latest package
        if rebuild:
            build_translations()

        request_url = "http://api.crowdin.net/api/project/%s/download/%s.zip?key=%s" % (project_id, lang_code, project_key)
        try:
            resp = requests.get(request_url)
            resp.raise_for_status()
        except Exception as e:
            if resp.status_code == 404:
                logging.info("No translations found for language %s" % lang_code)
                return None  # no translations
            elif resp.status_code == 401:
                raise CommandError("401 Unauthorized while trying to access the CrowdIn API. Be sure to set CROWDIN_PROJECT_ID and CROWDIN_PROJECT_KEY in local_settings.py.")
            else:
                raise CommandError("%s - couldn't connect to CrowdIn API - cannot continue without downloading %s!" % (e, request_url))
        else:
            logging.info("Successfully downloaded zip archive")

        # Unpack into temp dir
        try:
            z = zipfile.ZipFile(StringIO.StringIO(resp.content))
        except Exception as e:
            logging.error("Error downloading zip file: % s" % e)
            z = None

        try:
            if zip_file:
                with open(zip_file, "wb") as fp:  # save the zip file
                    fp.write(resp.content)
        except Exception as e:
            logging.error("Error writing zip file to %s: %s" % (zip_file, e))

    tmp_dir_path = tempfile.mkdtemp()
    if z:
        z.extractall(tmp_dir_path)

    # Copy over new translations
    po_file = extract_new_po(tmp_dir_path, combine_with_po_file=combine_with_po_file, lang=lang_code, filter_type=download_type)

    # Clean up tracks
    if os.path.exists(tmp_dir_path):
        shutil.rmtree(tmp_dir_path)

    return po_file


def build_translations(project_id=settings.CROWDIN_PROJECT_ID, project_key=settings.CROWDIN_PROJECT_KEY):
    """Build latest translations into zip archive on CrowdIn."""

    logging.info("Requesting that CrowdIn build a fresh zip of our translations")
    request_url = "http://api.crowdin.net/api/project/%s/export?key=%s" % (project_id, project_key)
    resp = requests.get(request_url)
    try:
        resp.raise_for_status()
    except Exception as e:
        logging.error(e)


def extract_new_po(extract_path, combine_with_po_file=None, lang="all", filter_type=None):
    """Move newly downloaded po files to correct location in locale
    direction. Returns the location of the po file if a single
    language is given, or a list of locations if language is
    'all'.

    """

    if combine_with_po_file:
        assert lang != 'all', "You can only combine a po file with only one other po file. Please select a specific language, not 'all'."
        assert os.path.basename(combine_with_po_file) in ["django.po", "djangojs.po"], "File %s does not seem to be either django.po or djangojs.po."

    if lang == 'all':
        languages = os.listdir(extract_path)
        return [extract_new_po(os.path.join(extract_path, l), lang=l) for l in languages]

    converted_code = lcode_to_django_dir(lang)

    def prep_inputs(extract_path, converted_code, filter_type):
        src_po_files = [po for po in all_po_files(extract_path)]

        # remove all exercise po that is not about math
        if filter_type:
            if filter_type == "ka":

                src_po_files = [os.path.splitext(po)[0] for po in src_po_files]
                src_po_files = filter(lambda fn: os.path.basename(fn).startswith("learn."), src_po_files)
                src_po_files = filter(lambda fn: ".videos" in fn or ".exercises" in fn or sum([po.startswith(fn[:-len(lang)-1]) for po in src_po_files]) > 1, src_po_files)
                src_po_files = [po + ".po" for po in src_po_files]

                # before we call msgcat, process each exercise po file and leave out only the metadata
                for exercise_po in get_exercise_po_files(src_po_files):
                    remove_exercise_nonmetadata(exercise_po)

        if combine_with_po_file:
            src_po_files.append(combine_with_po_file)

        return src_po_files
    src_po_files = prep_inputs(extract_path, converted_code, filter_type)


    def produce_outputs(src_po_files, converted_code):
        # ensure directory exists in locale folder, and then overwrite local po files with new ones
        dest_path = os.path.join(LOCALE_ROOT, converted_code, "LC_MESSAGES")
        ensure_dir(dest_path)
        dest_file = os.path.join(dest_path, 'django.po')

        if len(src_po_files) == 1:
            shutil.move(src_po_file[0], dest_file)

        else:
            build_file = os.path.join(dest_path, 'djangobuild.po')  # so we dont clobber previous django.po that we build

            logging.info('Concatenating all po files found...')
            try:
                concat_command = ['msgcat', '-o', build_file, '--no-location'] + src_po_files
                process = subprocess.Popen(concat_command, stderr=subprocess.STDOUT)
                process.wait()
                if not os.path.exists(build_file):
                    raise CommandError("Unable to concatenate po files.")
            except OSError as e:
                if e.strerror == "No such file or directory":
                    raise CommandError("%s must be installed and in your path to run this command." % concat_command[0])
                else:
                    raise
            shutil.move(build_file, dest_file)

        return dest_file

    dest_file = produce_outputs(src_po_files, converted_code)

    return dest_file


def get_po_metadata(pofilename):
    if not pofilename or not os.path.exists(pofilename):
        nphrases = 0
        ntranslations = 0
    else:
        pofile = polib.pofile(pofilename)
        nphrases = len(pofile)
        ntranslations = sum([int(po.msgid != po.msgstr) for po in pofile])

    return { "ntranslations": ntranslations, "nphrases": nphrases }


def remove_exercise_nonmetadata(pofilename):
    '''Checks each message block in the po file given by pofilename, and
    sees if the top comment of each one has the string '(of|for)
    exercise'. If not, then it will be deleted from the po file.
    '''
    assert os.path.exists(pofilename), "%s does not exist!" % pofilename

    EXERCISE_METADATA_LINE = r'.*(of|for) exercise <a'

    logging.info('Removing nonmetadata msgblocks from %s' % pofilename)
    pofile = polib.pofile(pofilename)

    clean_pofile = polib.POFile(encoding='utf-8')
    clean_pofile.append(pofile.metadata_as_entry())
    for msgblock in pofile:
        if 'Project-Id-Version' in msgblock.msgstr or msgblock.msgstr == '':  # is header; ignore, already included
            continue
        elif re.match(EXERCISE_METADATA_LINE, msgblock.tcomment):
            # is exercise metadata, preserve
            clean_pofile.append(msgblock)

    os.remove(pofilename)
    clean_pofile.save(fpath=pofilename)

    # ok, here's the deal: there's a bug right now in polib.py in which
    # it creates an empty header AUTOMATICALLY. Plus, there is no way
    # to specify what this header contains. So what do we do? We delete this
    # header. TODO for Aron: Fix polib.py
    with open(pofilename, 'r') as pofile:
        polines = pofile.read().split('\n')
    polines = '\n'.join(polines[4:])       # here the first 4 lines compose the empty header. Cull them!
    with open(pofilename, 'w') as fp:
        fp.write(polines)


def get_exercise_po_files(po_files):
    return fnmatch.filter(po_files, '*.exercises-*.po')


def all_po_files(dir):
    '''Walks the directory dir and returns an iterable containing all the
    po files in the given directory.

    '''
    # return glob.glob(os.path.join(dir, '*/*.po'))
    for current_dir, _, filenames in os.walk(dir):
        for po_file in fnmatch.filter(filenames, '*.po'):
            yield os.path.join(current_dir, po_file)


def generate_metadata(lang_codes=None, broken_langs=None, added_ka=False, package_metadata=None):
    """Loop through locale folder, create or update language specific meta
    and create or update master file, skipping broken languages

    note: broken_langs must be in django format.

    """
    logging.info("Generating new language pack metadata")

    lang_codes = lang_codes or os.listdir(LOCALE_ROOT)
    try:
        with open(get_language_pack_availability_filepath(), "r") as fp:
            master_metadata = json.load(fp)
    except Exception as e:
        logging.warn("Error opening language pack metadata: %s; resetting" % e)
        master_metadata = {}

    # loop through all languages in locale, update master file
    crowdin_meta_dict = download_crowdin_metadata()
    with open(SUBTITLE_COUNTS_FILEPATH, "r") as fp:
        subtitle_counts = json.load(fp)

    for lc in lang_codes:
        lang_code_django = lcode_to_django_dir(lc)
        lang_code_ietf = lcode_to_ietf(lc)
        lang_name = get_language_name(lang_code_ietf)

        # skips anything not a directory, or with errors
        if not os.path.isdir(os.path.join(LOCALE_ROOT, lang_code_django)):
            logging.info("Skipping item %s because it is not a directory" % lang_code_django)
            continue
        elif lang_code_django in broken_langs:  # broken_langs is django format
            logging.info("Skipping directory %s because it triggered an error during compilemessages. The admins should have received a report about this and must fix it before this pack will be updateed." % lang_code_django)
            continue

        # Gather existing metadata
        crowdin_meta = next((meta for meta in crowdin_meta_dict if meta["code"] == lang_code_ietf), {})
        metadata_filepath = get_language_pack_metadata_filepath(lang_code_ietf)
        try:
            with open(metadata_filepath) as fp:
                local_meta = json.load(fp)
        except Exception as e:
            logging.warn("Error opening language pack metadata (%s): %s; resetting" % (metadata_filepath, e))
            local_meta = {}

        try:
            lang_entry = package_metadata.get(lang_code_ietf, {})
            if "ntranslations" in lang_entry and "nphrases" in lang_entry:
                nphrases = lang_entry["nphrases"]
                ntranslations = lang_entry["ntranslations"]
                percent_translated = 100. * ntranslations / float(nphrases) if nphrases else 0  # for when language isn't even recognized

            else:
                nphrases = crowdin_meta.get("phrases", 0)
                ntranslations = crowdin_meta.get("approved", 0)
                percent_translated = crowdin_meta.get("approved_progress", 0)

            # update metadata
            updated_meta = {
                "code": lcode_to_ietf(crowdin_meta.get("code") or lang_code_django),  # user-facing code
                "name": (crowdin_meta.get("name") or lang_name),
                "percent_translated": percent_translated,
                "phrases": int(nphrases),
                "approved_translations": int(ntranslations),
            }

            # Obtain current number of subtitles
            entry = subtitle_counts.get(lang_name, {})
            srt_count = entry.get("count", 0)

            updated_meta.update({
                "software_version": version.VERSION,
                "subtitle_count": srt_count,
            })

        except LanguageNotFoundError:
            logging.error("Unrecognized language; must skip item %s" % lang_code_django)
            continue

        language_pack_version = increment_language_pack_version(local_meta, updated_meta)
        updated_meta["language_pack_version"] = language_pack_version + int(added_ka)
        local_meta.update(updated_meta)

        # Write locally (this is used on download by distributed server to update it's database)
        with open(metadata_filepath, 'w') as output:
            json.dump(local_meta, output)

        # Update master (this is used for central server to handle API requests for data)
        master_metadata[lang_code_ietf] = local_meta

    # Save updated master
    ensure_dir(os.path.dirname(get_language_pack_availability_filepath()))
    with open(get_language_pack_availability_filepath(), 'w') as output:
        json.dump(master_metadata, output)
    logging.info("Local record of translations updated")


def update_metadata(sizes):
    """
    We've zipped the packages, and now have unzipped & zipped sizes.
    Update this info in the local metadata (but not inside the zip)
    """
    try:
        with open(get_language_pack_availability_filepath(), "r") as fp:
            master_metadata = json.load(fp)
    except Exception as e:
        logging.warn("Error opening language pack metadata: %s; resetting" % e)
        master_metadata = {}

    for lc, sz in sizes.iteritems():
        lang_code_ietf = lcode_to_ietf(lc)

        # Gather existing metadata
        metadata_filepath = get_language_pack_metadata_filepath(lang_code_ietf)
        try:
            with open(metadata_filepath) as fp:
                local_meta = json.load(fp)
        except Exception as e:
            logging.warn("Error opening language pack metadata (%s): %s; resetting" % (metadata_filepath, e))
            continue

        local_meta["package_size"] = sz["package_size"]
        local_meta["zip_size"] = sz["zip_size"]

        # Write locally (this is used on download by distributed server to update it's database)
        with open(metadata_filepath, 'w') as output:
            json.dump(local_meta, output)

        # Update master (this is used for central server to handle API requests for data)
        master_metadata[lang_code_ietf] = local_meta

    # Save updated master
    ensure_dir(os.path.dirname(get_language_pack_availability_filepath()))
    with open(get_language_pack_availability_filepath(), 'w') as output:
        json.dump(master_metadata, output)
    logging.info("Local record of translations updated")


def download_crowdin_metadata(project_id=settings.CROWDIN_PROJECT_ID, project_key=settings.CROWDIN_PROJECT_KEY):
    """Return tuple in format (total_strings, total_translated, percent_translated)"""

    request_url = "http://api.crowdin.net/api/project/%s/status?key=%s&json=True" % (project_id, project_key)
    resp = requests.get(request_url)
    resp.raise_for_status()

    crowdin_meta_dict = json.loads(resp.content)
    return crowdin_meta_dict


def increment_language_pack_version(local_meta, updated_meta):
    """Increment language pack version if translations have been updated
(start over if software version has incremented)
    """
    if not local_meta or version_diff(local_meta.get("software_version"), version.VERSION) < 0:
        # set to one for the first time, or if this is the first build of a new software version
        language_pack_version = 1
    elif local_meta.get("total_translated") == updated_meta.get("approved") and local_meta.get("subtitle_count") == updated_meta.get("subtitle_count"):
        language_pack_version = local_meta.get("language_pack_version") or 1
    else:
        language_pack_version = local_meta.get("language_pack_version") + 1
    return language_pack_version


def zip_language_packs(lang_codes=None):
    """Zip up and expose all language packs

    converts all into ietf
    """
    sizes = {}
    lang_codes = lang_codes or os.listdir(LOCALE_ROOT)
    lang_codes = [lcode_to_ietf(lc) for lc in lang_codes]
    logging.info("Zipping up %d language pack(s)" % len(lang_codes))

    for lang_code_ietf in lang_codes:
        lang_code_django = lcode_to_django_dir(lang_code_ietf)
        lang_locale_path = os.path.join(LOCALE_ROOT, lang_code_django)
        sizes[lang_code_ietf] = { "package_size": 0, "zip_size": 0}

        if not os.path.exists(lang_locale_path):
            logging.warn("Unexpectedly skipping missing directory: %s" % lang_code_django)
        elif not os.path.isdir(lang_locale_path):
            logging.error("Skipping language where a file exists where a directory was expected: %s" % lang_code_django)

        # Create a zipfile for this language
        zip_filepath = get_language_pack_filepath(lang_code_ietf)
        ensure_dir(os.path.dirname(zip_filepath))
        logging.info("Creating zip file in %s" % zip_filepath)
        z = zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED)

        # Get every single file in the directory and zip it up
        for metadata_file in glob.glob('%s/*.json' % lang_locale_path):
            filepath = os.path.join(lang_locale_path, metadata_file)
            z.write(filepath, arcname=os.path.basename(metadata_file))
            sizes[lang_code_ietf]["package_size"] += os.path.getsize(filepath)

        for mo_file in glob.glob('%s/LC_MESSAGES/*.mo' % lang_locale_path):
            filepath = os.path.join(lang_locale_path, mo_file)
            z.write(filepath, arcname=os.path.join("LC_MESSAGES", os.path.basename(mo_file)))
            sizes[lang_code_ietf]["package_size"] += os.path.getsize(filepath)

        for srt_file in glob.glob('%s/subtitles/*.srt' % lang_locale_path):
            filepath = os.path.join(lang_locale_path, srt_file)
            z.write(filepath, arcname=os.path.join("subtitles", os.path.basename(srt_file)))
            sizes[lang_code_ietf]["package_size"] += os.path.getsize(filepath)

        z.close()
        sizes[lang_code_ietf]["zip_size"]= os.path.getsize(zip_filepath)

    logging.info("Done.")
    return sizes
