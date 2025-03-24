"""
This script accepts a PDF document filename and converts it to a text file
in Markdown format, compatible with the GitHub standard.

It must be invoked with the filename like this:

python pymupdf_rag.py input.pdf [-pages PAGES]

The "PAGES" parameter is a string (containing no spaces) of comma-separated
page numbers to consider. Each item is either a single page number or a
number range "m-n". Use "N" to address the document's last page number.
Example: "-pages 2-15,40,43-N"

It will produce a markdown text file called "input.md".

Text will be sorted in Western reading order. Any table will be included in
the text in markdwn format as well.

Dependencies
-------------
PyMuPDF v1.25.4 or later

Copyright and License
----------------------
Copyright (C) 2024-2025 Artifex Software, Inc.

PyMuPDF4LLM is free software: you can redistribute it and/or modify it under the
terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version.

Alternative licensing terms are available from the licensor.
For commercial licensing, see <https://www.artifex.com/> or contact
Artifex Software, Inc., 39 Mesa Street, Suite 108A, San Francisco,
CA 94129, USA, for further information.
"""

import os
import string
from binascii import b2a_base64
import pymupdf
from pymupdf4llm.helpers.get_text_lines import get_raw_lines, is_white
from pymupdf4llm.helpers.multi_column import column_boxes
from pymupdf4llm.helpers.progress import ProgressBar
from dataclasses import dataclass

pymupdf.TOOLS.unset_quad_corrections(True)
# Characters recognized as bullets when starting a line.
bullet = tuple(
    [
        "- ",
        "* ",
        "> ",
        chr(0xB6),
        chr(0xB7),
        chr(8224),
        chr(8225),
        chr(8226),
        chr(0xF0A7),
        chr(0xF0B7),
    ]
    + list(map(chr, range(9632, 9680)))
)

GRAPHICS_TEXT = "\n![](%s)\n"


class IdentifyHeaders:
    """Compute data for identifying header text.

    All non-white text from all selected pages is extracted and its font size
    noted as a rounded value.
    The most frequent font size (and all smaller ones) is taken as body text
    font size.
    Larger font sizes are mapped to strings of multiples of '#', the header
    tag in Markdown, which in turn is Markdown's representation of HTML's
    header tags <h1> to <h6>.
    Larger font sizes than body text but smaller than the <h6> font size are
    represented as <h6>.
    """

    def __init__(
        self,
        doc: str,
        pages: list = None,
        body_limit: float = 12,
    ):
        """Read all text and make a dictionary of fontsizes.

        Args:
            pages: optional list of pages to consider
            body_limit: consider text with larger font size as some header
        """
        if isinstance(doc, pymupdf.Document):
            mydoc = doc
        else:
            mydoc = pymupdf.open(doc)

        if pages is None:  # use all pages if omitted
            pages = range(mydoc.page_count)

        fontsizes = {}
        for pno in pages:
            page = mydoc.load_page(pno)
            blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
            for span in [  # look at all non-empty horizontal spans
                s
                for b in blocks
                for l in b["lines"]
                for s in l["spans"]
                if not is_white(s["text"])
            ]:
                fontsz = round(span["size"])
                count = fontsizes.get(fontsz, 0) + len(span["text"].strip())
                fontsizes[fontsz] = count

        if mydoc != doc:
            # if opened here, close it now
            mydoc.close()

        # maps a fontsize to a string of multiple # header tag characters
        self.header_id = {}

        # If not provided, choose the most frequent font size as body text.
        # If no text at all on all pages, just use 12.
        # In any case all fonts not exceeding
        temp = sorted(
            [(k, v) for k, v in fontsizes.items()],
            key=lambda i: i[1],
            reverse=True,
        )
        if temp:
            self.body_limit = min(body_limit, temp[0][0])
        else:
            self.body_limit = body_limit

        # identify up to 6 font sizes as header candidates
        sizes = sorted(
            [f for f in fontsizes.keys() if f > self.body_limit],
            reverse=True,
        )[:6]

        # make the header tag dictionary
        for i, size in enumerate(sizes):
            self.header_id[size] = "#" * (i + 1) + " "

    def get_header_id(self, span: dict, page=None) -> str:
        """Return appropriate markdown header prefix.

        Given a text span from a "dict"/"rawdict" extraction, determine the
        markdown header prefix string of 0 to n concatenated '#' characters.
        """
        fontsize = round(span["size"])  # compute fontsize
        if fontsize <= self.body_limit:  # shortcut for body text
            return ""
        hdr_id = self.header_id.get(fontsize, "")
        # If no header but larger than body text, assign <h6>.
        if not hdr_id and fontsize > self.body_limit:
            hdr_id = "###### "
        return hdr_id


# store relevant parameters here
@dataclass
class Parameters:
    pass


def refine_boxes(boxes, enlarge=0):
    """Join any rectangles with a pairwise non-empty overlap.

    Accepts and returns a list of Rect items.
    Note that rectangles that only "touch" each other (common point or edge)
    are not considered as overlapping.
    Use a positive "enlarge" parameter to enlarge rectangle by these many
    points in every direction.

    TODO: Consider using a sweeping line algorithm for this.
    """
    delta = (-enlarge, -enlarge, enlarge, enlarge)
    new_rects = []
    # list of all vector graphic rectangles
    prects = boxes[:]

    while prects:  # the algorithm will empty this list
        r = +prects[0] + delta  # copy of first rectangle
        repeat = True  # initialize condition
        while repeat:
            repeat = False  # set false as default
            for i in range(len(prects) - 1, 0, -1):  # from back to front
                if r.intersects(prects[i].irect):  # enlarge first rect with this
                    r |= prects[i]
                    del prects[i]  # delete this rect
                    repeat = True  # indicate must try again

        # first rect now includes all overlaps
        new_rects.append(r)
        del prects[0]

    new_rects = sorted(set(new_rects), key=lambda r: (r.x0, r.y0))
    return new_rects


def is_significant(box, paths):
    """Check whether the rectangle "box" contains 'signifiant' drawings.

    This means that some path is contained in the "interior" of box.
    To this end, we build a sub-box of 90% of the original box and check
    whether this still contains drawing paths.
    """
    if box.width > box.height:
        d = box.width * 0.025
    else:
        d = box.height * 0.025
    nbox = box + (d, d, -d, -d)  # nbox covers 90% of box interior
    # paths contained in box:
    my_paths = [p for p in paths if p["rect"] in box and p["rect"] != box]
    for p in my_paths:
        rect = p["rect"]
        if not (rect & nbox).is_empty:  # intersects interior: significant!
            return True
        # Remaining case: a horizontal or vertical line
        # horizontal line:
        if rect.y0 == rect.y1 and rect.x0 < nbox.x1 and rect.x1 > nbox.x0:
            return True
        # vertical line
        if rect.x0 == rect.x1 and rect.y0 < nbox.y1 and rect.y1 > nbox.y0:
            return True
    return False


def to_markdown(
    doc,
    *,
    pages=None,
    hdr_info=None,
    write_images=False,
    embed_images=False,
    image_path="",
    image_format="png",
    image_size_limit=0.05,
    filename=None,
    force_text=True,
    page_chunks=False,
    margins=(0, 0, 0, 0),
    dpi=150,
    page_width=612,
    page_height=None,
    table_strategy="lines_strict",
    graphics_limit=None,
    fontsize_limit=3,
    ignore_code=False,
    extract_words=False,
    show_progress=False,
):
    """Process the document and return the text of the selected pages.

    Args:
        doc: pymupdf.Document or string.
        pages: list of page numbers to consider (0-based).
        hdr_info: callable or object having a method named 'get_hdr_info'.
        write_images: (bool) whether to save images / drawing as files.
        embed_images: (bool) embed images as base64 encoded strings
        image_path: (str) folder into which images should be stored.
        image_format: (str) desired image format. Choose a supported one.
        force_text: (bool) output text despite of background.
        page_chunks: (bool) whether to segment output by page.
        margins: do not consider content overlapping margin areas.
        dpi: (int) desired resolution for generated images.
        page_width: (float) assumption if page layout is variable.
        page_height: (float) assumption if page layout is variable.
        table_strategy: choose table detection strategy
        graphics_limit: (int) ignore page with too many vector graphics.
        ignore_code: (bool) suppress code-like formatting (mono-space fonts)
        extract_words: (bool) include "words"-like output in page chunks
        show_progress: (bool) print progress as each page is processed.

    """
    if write_images is False and embed_images is False and force_text is False:
        raise ValueError("Image and text on images cannot both be suppressed.")
    if embed_images is True:
        write_images = False
        image_path = ""
    if not 0 <= image_size_limit < 1:
        raise ValueError("'image_size_limit' must be non-negative and less than 1.")
    DPI = dpi
    IGNORE_CODE = ignore_code
    IMG_EXTENSION = image_format
    EXTRACT_WORDS = extract_words
    if EXTRACT_WORDS is True:
        page_chunks = True
        ignore_code = True
    IMG_PATH = image_path
    if IMG_PATH and write_images is True and not os.path.exists(IMG_PATH):
        os.mkdir(IMG_PATH)

    if not isinstance(doc, pymupdf.Document):
        doc = pymupdf.open(doc)

    FILENAME = doc.name if filename is None else filename
    GRAPHICS_LIMIT = graphics_limit
    FONTSIZE_LIMIT = fontsize_limit

    # for reflowable documents allow making 1 page for the whole document
    if doc.is_reflowable:
        if hasattr(page_height, "__float__"):
            # accept user page dimensions
            doc.layout(width=page_width, height=page_height)
        else:
            # no page height limit given: make 1 page for whole document
            doc.layout(width=page_width, height=792)
            page_count = doc.page_count
            height = 792 * page_count  # height that covers full document
            doc.layout(width=page_width, height=height)

    if pages is None:  # use all pages if no selection given
        pages = list(range(doc.page_count))

    if hasattr(margins, "__float__"):
        margins = [margins] * 4
    if len(margins) == 2:
        margins = (0, margins[0], 0, margins[1])
    if len(margins) != 4:
        raise ValueError("margins must be one, two or four floats")
    elif not all([hasattr(m, "__float__") for m in margins]):
        raise ValueError("margin values must be floats")

    # If "hdr_info" is not an object with a method "get_header_id", scan the
    # document and use font sizes as header level indicators.
    if callable(hdr_info):
        get_header_id = hdr_info
    elif hasattr(hdr_info, "get_header_id") and callable(hdr_info.get_header_id):
        get_header_id = hdr_info.get_header_id
    elif hdr_info is False:
        get_header_id = lambda s, page=None: ""
    else:
        hdr_info = IdentifyHeaders(doc)
        get_header_id = hdr_info.get_header_id

    def resolve_links(links, span):
        """Accept a span and return a markdown link string.

        Args:
            links: a list as returned by page.get_links()
            span: a span dictionary as returned by page.get_text("dict")

        Returns:
            None or a string representing the link in MD format.
        """
        bbox = pymupdf.Rect(span["bbox"])  # span bbox
        # a link should overlap at least 70% of the span
        for link in links:
            hot = link["from"]  # the hot area of the link
            middle = (hot.tl + hot.br) / 2  # middle point of hot area
            if not middle in bbox:
                continue  # does not touch the bbox
            text = f'[{span["text"].strip()}]({link["uri"]})'
            return text

    def save_image(parms, rect, i):
        """Optionally render the rect part of a page.

        We will ignore images that are empty or that have an edge smaller
        than x% of the corresponding page edge."""
        page = parms.page
        if (
            rect.width < page.rect.width * image_size_limit
            or rect.height < page.rect.height * image_size_limit
        ):
            return ""
        if write_images is True or embed_images is True:
            pix = page.get_pixmap(clip=rect, dpi=DPI)
        else:
            return ""
        if pix.height <= 0 or pix.width <= 0:
            return ""

        if write_images is True:
            filename = os.path.basename(parms.filename).replace(" ", "-")
            image_filename = os.path.join(
                IMG_PATH, f"{filename}-{page.number}-{i}.{IMG_EXTENSION}"
            )
            pix.save(image_filename)
            return image_filename.replace("\\", "/")
        elif embed_images is True:
            # make a base64 encoded string of the image
            data = b2a_base64(pix.tobytes(IMG_EXTENSION)).decode()
            data = f"data:image/{IMG_EXTENSION};base64," + data
            return data
        return ""

    def write_text(
        parms,
        clip: pymupdf.Rect,
        tables=True,
        images=True,
        force_text=force_text,
    ):
        """Output the text found inside the given clip.

        This is an alternative for plain text in that it outputs
        text enriched with markdown styling.
        The logic is capable of recognizing headers, body text, code blocks,
        inline code, bold, italic and bold-italic styling.
        There is also some effort for list supported (ordered / unordered) in
        that typical characters are replaced by respective markdown characters.

        'tables'/'images' indicate whether this execution should output these
        objects.
        """

        if clip is None:
            clip = parms.clip
        out_string = ""
        # This is a list of tuples (linerect, spanlist)
        nlines = [
            l
            for l in get_raw_lines(parms.textpage, clip=clip, tolerance=3)
            if not intersects_rects(l[0], parms.tab_rects.values())
        ]

        parms.line_rects.extend(
            [l[0] for l in nlines if not intersects_rects(l[0], parms.tab_rects.values())]
        )  # store line rectangles

        prev_lrect = None  # previous line rectangle
        prev_bno = -1  # previous block number of line
        code = False  # mode indicator: outputting code
        prev_hdr_string = None

        for lrect, spans in nlines:
            # there may be tables or images inside the text block: skip them
            if intersects_rects(lrect, parms.img_rects):
                continue

            # ------------------------------------------------------------
            # Pick up tables ABOVE this text block
            # ------------------------------------------------------------
            if tables:
                for i, _ in sorted(
                    [
                        j
                        for j in parms.tab_rects.items()
                        if j[1].y1 <= lrect.y0 and not (j[1] & lrect).is_empty
                    ],
                    key=lambda j: (j[1].y1, j[1].x0),
                ):
                    out_string += "\n" + parms.tabs[i].to_markdown(clean=False) + "\n"
                    if EXTRACT_WORDS:
                        # for "words" extraction, add table cells as line rects
                        cells = sorted(
                            set(
                                [
                                    pymupdf.Rect(c)
                                    for c in parms.tabs[i].header.cells
                                    + parms.tabs[i].cells
                                    if c is not None
                                ]
                            ),
                            key=lambda c: (c.y1, c.x0),
                        )
                        parms.line_rects.extend(cells)
                    del tab_rects[i]

            # ------------------------------------------------------------
            # Pick up images / graphics ABOVE this text block
            # ------------------------------------------------------------
            if images:
                for i in range(len(parms.img_rects)):
                    if i in parms.deleted_images:
                        continue
                    r = parms.img_rects[i]
                    if r.y1 <= lrect.y0 and not (r & lrect).is_empty:
                        pathname = save_image(parms, r, i)
                        if pathname:
                            out_string += GRAPHICS_TEXT % pathname

                        # recursive invocation
                        if force_text:
                            img_txt = write_text(
                                parms,
                                r,
                                tables=False,
                                images=False,
                                force_text=True,
                            )

                            if not is_white(img_txt):
                                out_string += img_txt
                        parms.deleted_images.append(i)

            parms.line_rects.append(lrect)
            text = " ".join([s["text"] for s in spans])

            # full line mono-spaced?
            if not IGNORE_CODE:
                all_mono = all([s["flags"] & 8 for s in spans])
            else:
                all_mono = False

            if all_mono:
                if not code:  # if not already in code output mode:
                    out_string += "```\n"  # switch on "code" mode
                    code = True
                # compute approx. distance from left - assuming a width
                # of 0.5*fontsize.
                delta = int((lrect.x0 - clip.x0) / (spans[0]["size"] * 0.5))
                indent = " " * delta

                out_string += indent + text + "\n"
                continue  # done with this line

            span0 = spans[0]
            bno = span0["block"]  # block number of line
            if bno != prev_bno:
                out_string += "\n"
                prev_bno = bno

            if (  # check if we need another line break
                prev_lrect
                and lrect.y1 - prev_lrect.y1 > lrect.height * 1.5
                or span0["text"].startswith("[")
                or span0["text"].startswith(bullet)
                or span0["flags"] & 1  # superscript?
            ):
                out_string += "\n"
            prev_lrect = lrect

            # if line is a header, this will return multiple "#" characters
            hdr_string = get_header_id(span0, page=parms.page)

            # intercept if header text has been broken in multiple lines
            if hdr_string and hdr_string == prev_hdr_string:
                while out_string.endswith("\n"):
                    out_string = out_string[:-1]
                out_string = out_string[:-1] + " " + text + "\n"
                continue

            prev_hdr_string = hdr_string
            if hdr_string.startswith("#"):  # if a header line skip the rest
                out_string += hdr_string + text + "\n"
                continue

            # this line is not all-mono, so switch off "code" mode
            if code:  # in code output mode?
                out_string += "```\n"  # switch of code mode
                code = False

            for i, s in enumerate(spans):  # iterate spans of the line
                # decode font properties
                mono = s["flags"] & 8 and IGNORE_CODE is False
                bold = s["flags"] & 16
                italic = s["flags"] & 2

                if mono:
                    # this is text in some monospaced font
                    out_string += f"`{s['text'].strip()}` "
                else:  # not a mono text
                    prefix = ""
                    suffix = ""
                    if hdr_string == "":
                        if bold:
                            prefix = "**"
                            suffix += "**"
                        if italic:
                            prefix += "_"
                            suffix = "_" + suffix

                    # convert intersecting link to markdown syntax
                    ltext = resolve_links(parms.links, s)
                    if ltext:
                        text = f"{hdr_string}{prefix}{ltext}{suffix} "
                    else:
                        text = f"{hdr_string}{prefix}{s['text'].strip()}{suffix} "
                    if text.startswith(bullet):
                        text = text[1:]
                        if len(text) > 1 and text[1] == " ":
                            t = "-"
                        else:
                            t = "- "
                        text = t + text[1:]
                        dist = span0["bbox"][0] - clip.x0
                        cwidth = (span0["bbox"][2] - span0["bbox"][0]) / len(
                            span0["text"]
                        )
                        if cwidth == 0:
                            cwidth = 1
                        text = " " * int(round(dist / cwidth)) + text

                    out_string += text
            if not code:
                out_string += "\n"
        out_string += "\n"
        if code:
            out_string += "```\n"  # switch of code mode
            code = False

        return (
            out_string.replace(" \n", "\n").replace("  ", " ").replace("\n\n\n", "\n\n")
        )

    def is_in_rects(rect, rect_list):
        """Check if rect is contained in a rect of the list."""
        for i, r in enumerate(rect_list, start=1):
            if rect in r:
                return i
        return 0

    def intersects_rects(rect, rect_list):
        """Check if middle of rect is contained in a rect of the list."""
        delta = (-1, -1, 1, 1)  # enlarge rect_list members somewhat by this
        for i, r in enumerate(rect_list, start=1):
            if (rect.tl + rect.br) / 2 in r + delta:  # middle point is inside r
                return i
        return 0

    def output_tables(parms, text_rect):
        """Output tables above given text rectangle."""
        this_md = ""  # markdown string for table(s) content
        if text_rect is not None:  # select tables above the text block
            for i, trect in sorted(
                [j for j in parms.tab_rects.items() if j[1].y1 <= text_rect.y0],
                key=lambda j: (j[1].y1, j[1].x0),
            ):
                this_md += parms.tabs[i].to_markdown(clean=False)
                if EXTRACT_WORDS:
                    # for "words" extraction, add table cells as line rects
                    cells = sorted(
                        set(
                            [
                                pymupdf.Rect(c)
                                for c in parms.tabs[i].header.cells
                                + parms.tabs[i].cells
                                if c is not None
                            ]
                        ),
                        key=lambda c: (c.y1, c.x0),
                    )
                    parms.line_rects.extend(cells)
                del parms.tab_rects[i]  # do not touch this table twice

        else:  # output all remaining tables
            for i, trect in sorted(
                parms.tab_rects.items(),
                key=lambda j: (j[1].y1, j[1].x0),
            ):
                this_md += parms.tabs[i].to_markdown(clean=False)
                if EXTRACT_WORDS:
                    # for "words" extraction, add table cells as line rects
                    cells = sorted(
                        set(
                            [
                                pymupdf.Rect(c)
                                for c in parms.tabs[i].header.cells
                                + parms.tabs[i].cells
                                if c is not None
                            ]
                        ),
                        key=lambda c: (c.y1, c.x0),
                    )
                    parms.line_rects.extend(cells)
                del parms.tab_rects[i]  # do not touch this table twice
        return this_md

    def output_images(parms, text_rect):
        """Output images and graphics above text rectangle."""
        if not parms.img_rects:
            return ""
        this_md = ""  # markdown string
        if text_rect is not None:  # select images above the text block
            for i, img_rect in enumerate(parms.img_rects):
                if img_rect.y0 > text_rect.y0:
                    continue
                if img_rect.x0 >= text_rect.x1 or img_rect.x1 <= text_rect.x0:
                    continue
                if i in parms.deleted_images:
                    continue
                pathname = save_image(parms, img_rect, i)
                parms.deleted_images.append(i)  # do not touch this image twice
                if pathname:
                    this_md += GRAPHICS_TEXT % pathname
                if force_text:
                    img_txt = write_text(
                        parms,
                        img_rect,
                        tables=False,  # we have no tables here
                        images=False,  # we have no other images here
                        force_text=True,
                    )
                    if not is_white(img_txt):  # was there text at all?
                        this_md += img_txt
        else:  # output all remaining images
            for i, img_rect in enumerate(parms.img_rects):
                if i in parms.deleted_images:
                    continue
                pathname = save_image(parms, img_rect, i)
                parms.deleted_images.append(i)  # do not touch this image twice
                if pathname:
                    this_md += GRAPHICS_TEXT % pathname
                if force_text:
                    img_txt = write_text(
                        parms,
                        img_rect,
                        tables=False,  # we have no tables here
                        images=False,  # we have no other images here
                        force_text=True,
                    )
                    if not is_white(img_txt):
                        this_md += img_txt

        return this_md

    def get_bg_color(page):
        """Determine the background color of the page.

        The function returns a PDF RGB color triple or None.
        We check the color of 10 x 10 pixel areas in the four corners of the
        page. If they are unicolor and of the same color, we assume this to
        be the background color.
        """
        pix = page.get_pixmap(clip=(0, 0, 10, 10))
        if not pix.is_unicolor:
            return None
        pixel_ul = pix.pixel(0, 0)  # upper left color
        pix = page.get_pixmap(clip=(page.rect.width - 10, 0, page.rect.width, 10))
        if not pix.is_unicolor:
            return None
        pixel_ur = pix.pixel(0, 0)  # upper right color
        if not pixel_ul == pixel_ur:
            return None
        pix = page.get_pixmap(clip=(0, page.rect.height - 10, 10, page.rect.height))
        if not pix.is_unicolor:
            return None
        pixel_ll = pix.pixel(0, 0)  # lower left color
        if not pixel_ul == pixel_ll:
            return None
        pix = page.get_pixmap(
            clip=(
                page.rect.width - 10,
                page.rect.height - 10,
                page.rect.width,
                page.rect.height,
            )
        )
        if not pix.is_unicolor:
            return None
        pixel_lr = pix.pixel(0, 0)  # lower right color
        if not pixel_ul == pixel_lr:
            return None
        return (pixel_ul[0] / 255, pixel_ul[1] / 255, pixel_ul[2] / 255)

    def get_metadata(doc, pno):
        meta = doc.metadata.copy()
        meta["file_path"] = FILENAME
        meta["page_count"] = doc.page_count
        meta["page"] = pno + 1
        return meta

    def sort_words(words: list) -> list:
        """Reorder words in lines.

        The argument list must be presorted by bottom, then left coordinates.

        Words with similar top / bottom coordinates are assumed to belong to
        the same line and will be sorted left to right within that line.
        """
        if not words:
            return []
        nwords = []
        line = [words[0]]
        lrect = pymupdf.Rect(words[0][:4])
        for w in words[1:]:
            if abs(w[1] - lrect.y0) <= 3 or abs(w[3] - lrect.y1) <= 3:
                line.append(w)
                lrect |= w[:4]
            else:
                line.sort(key=lambda w: w[0])
                nwords.extend(line)
                line = [w]
                lrect = pymupdf.Rect(w[:4])
        line.sort(key=lambda w: w[0])
        nwords.extend(line)
        return nwords

    def get_page_output(doc, pno, margins, textflags, FILENAME):
        """Process one page.

        Args:
            doc: pymupdf.Document
            pno: 0-based page number
            textflags: text extraction flag bits

        Returns:
            Markdown string of page content and image, table and vector
            graphics information.
        """
        page = doc[pno]
        page.remove_rotation()  # make sure we work on rotation=0
        parms = Parameters()  # all page information
        parms.page = page
        parms.filename = FILENAME
        parms.md_string = ""
        parms.images = []
        parms.tables = []
        parms.graphics = []
        parms.words = []
        parms.line_rects = []
        # determine background color
        parms.bg_color = get_bg_color(page)
        # catch too-many-graphics situation
        if GRAPHICS_LIMIT is not None:
            test_paths = page.get_cdrawings()  # fastest access to graphics
            if (excess := len(test_paths)) > GRAPHICS_LIMIT:
                parms.md_string = (
                    f"\n**Ignoring page {page.number} with {excess}+ vector graphics.**"
                )
                parms.md_string += "\n\n-----\n\n"
                return parms
        left, top, right, bottom = margins
        parms.clip = page.rect + (left, top, -right, -bottom)

        # extract external links on page
        parms.links = [l for l in page.get_links() if l["kind"] == pymupdf.LINK_URI]

        # make a TextPage for all later extractions
        parms.textpage = page.get_textpage(flags=textflags, clip=parms.clip)

        # extract images on page
        img_info = page.get_image_info()
        for i in range(len(img_info)):
            item = img_info[i]
            bbox = pymupdf.Rect(item["bbox"]) & parms.clip
            item["bbox"] = +bbox
            img_info[i] = item
        img_info = [
            i
            for i in img_info
            if i["bbox"].width >= image_size_limit * parms.clip.width
            and i["bbox"].height >= image_size_limit * parms.clip.height
        ]
        # sort descending by image area size
        img_info.sort(key=lambda i: abs(i["bbox"]), reverse=True)
        img_info = img_info[:30]  # only accept the largest up to 30 images
        # run from back to front (= small to large)
        for i in range(len(img_info) - 1, 0, -1):
            r = img_info[i]["bbox"]
            if r.is_empty:
                del img_info[i]
                continue
            for j in range(i):  # image areas larger than r
                if r in img_info[j]["bbox"]:
                    del img_info[i]  # contained in some larger image
                    break
        parms.images = img_info
        parms.img_rects = [i["bbox"] for i in parms.images]

        # Locate all tables on page
        parms.tabs = page.find_tables(clip=parms.clip, strategy=table_strategy)
        # Make a list of table boundary boxes.
        # Must include the header bbox (which may exist outside tab.bbox)
        tab_rects = {}
        for i, t in enumerate(parms.tabs):
            tab_rects[i] = pymupdf.Rect(t.bbox) | pymupdf.Rect(t.header.bbox)
            tab_dict = {
                "bbox": tuple(tab_rects[i]),
                "rows": t.row_count,
                "columns": t.col_count,
            }
            parms.tables.append(tab_dict)
        parms.tab_rects = tab_rects
        # list of table rectangles
        parms.tab_rects0 = list(tab_rects.values())

        # Select paths not intersecting any table.
        # Ignore full page graphics.
        # Ignore fill paths having the background color.
        paths = [
            p
            for p in page.get_drawings()
            if not intersects_rects(p["rect"], parms.tab_rects0)
            and p["rect"] in parms.clip
            and 3 < p["rect"].width < parms.clip.width
            and 3 < p["rect"].height < parms.clip.height
            and not (p["type"] == "f" and p["fill"] == parms.bg_color)
        ]

        # We also ignore vector graphics that only represent
        # "text emphasizing sugar".
        vg_clusters0 = []  # worthwhile vector graphics go here

        # walk through all vector graphics outside any table
        for bbox in refine_boxes(page.cluster_drawings(drawings=paths)):
            if is_significant(bbox, paths):
                vg_clusters0.append(bbox)

        # remove paths that are not in some relevant graphic
        parms.actual_paths = [p for p in paths if is_in_rects(p["rect"], vg_clusters0)]

        # also add image rectangles to the list
        vg_clusters0.extend(parms.img_rects)
        parms.img_rects.extend(vg_clusters0)
        parms.img_rects = sorted(set(parms.img_rects), key=lambda r: (r.y1, r.x0))
        parms.deleted_images = []
        # these may no longer be pairwise disjoint:
        # remove area overlaps by joining into larger rects
        parms.vg_clusters0 = refine_boxes(vg_clusters0)

        parms.vg_clusters = dict((i, r) for i, r in enumerate(parms.vg_clusters0))

        # identify text bboxes on page, avoiding tables, images and graphics
        text_rects = column_boxes(
            parms.page,
            paths=parms.actual_paths,
            no_image_text=True,
            textpage=parms.textpage,
            avoid=parms.tab_rects0 + parms.vg_clusters0,
            footer_margin=margins[3],
            header_margin=margins[1],
        )

        """
        ------------------------------------------------------------------
        Extract markdown text iterating over text rectangles.
        We also output any tables. They may live above, below or inside
        the text rectangles.
        ------------------------------------------------------------------
        """
        for text_rect in text_rects:
            # output tables above this rectangle
            parms.md_string += output_tables(parms, text_rect)
            parms.md_string += output_images(parms, text_rect)

            # output text inside this rectangle
            parms.md_string += write_text(
                parms,
                text_rect,
                force_text=force_text,
                images=True,
                tables=True,
            )

        parms.md_string = parms.md_string.replace(" ,", ",").replace("-\n", "")

        # write any remaining tables and images
        parms.md_string += output_tables(parms, None)
        parms.md_string += output_images(parms, None)

        parms.md_string += "\n-----\n\n"
        while parms.md_string.startswith("\n"):
            parms.md_string = parms.md_string[1:]
        parms.md_string = parms.md_string.replace(chr(0), chr(0xFFFD))

        if EXTRACT_WORDS is True:
            # output words in sequence compliant with Markdown text
            rawwords = parms.textpage.extractWORDS()
            rawwords.sort(key=lambda w: (w[3], w[0]))

            words = []
            for lrect in parms.line_rects:
                lwords = []
                for w in rawwords:
                    wrect = pymupdf.Rect(w[:4])
                    if wrect in lrect:
                        lwords.append(w)
                words.extend(sort_words(lwords))

            # remove word duplicates without spoiling the sequence
            # duplicates may occur for multiple reasons
            nwords = []  # words w/o duplicates
            for w in words:
                if w not in nwords:
                    nwords.append(w)
            words = nwords

        else:
            words = []
        parms.words = words
        return parms

    if page_chunks is False:
        document_output = ""
    else:
        document_output = []

    # read the Table of Contents
    toc = doc.get_toc()

    textflags = pymupdf.TEXT_MEDIABOX_CLIP | pymupdf.TEXT_ACCURATE_BBOXES

    if show_progress:
        print(f"Processing {FILENAME}...")
        pages = ProgressBar(pages)
    for pno in pages:
        parms = get_page_output(doc, pno, margins, textflags, FILENAME)
        if page_chunks is False:
            document_output += parms.md_string
        else:
            # build subet of TOC for this page
            page_tocs = [t for t in toc if t[-1] == pno + 1]

            metadata = get_metadata(doc, pno)
            document_output.append(
                {
                    "metadata": metadata,
                    "toc_items": page_tocs,
                    "tables": parms.tables,
                    "images": parms.images,
                    "graphics": parms.graphics,
                    "text": parms.md_string,
                    "words": parms.words,
                }
            )
        del parms

    return document_output


if __name__ == "__main__":
    import pathlib
    import sys
    import time

    try:
        filename = (
            "C:/Users/haral/OneDrive/Desktop/pymupdf4llm/issues/0225/e000050.full.pdf"
        )
    except IndexError:
        print(f"Usage:\npython {os.path.basename(__file__)} input.pdf")
        sys.exit()

    t0 = time.perf_counter()  # start a time

    doc = pymupdf.open(filename)  # open input file
    parms = sys.argv[2:]  # contains ["-pages", "PAGES"] or empty list
    pages = range(doc.page_count)  # default page range
    if len(parms) == 2 and parms[0] == "-pages":  # page sub-selection given
        pages = []  # list of desired page numbers

        # replace any variable "N" by page count
        pages_spec = parms[1].replace("N", f"{doc.page_count}")
        for spec in pages_spec.split(","):
            if "-" in spec:
                start, end = map(int, spec.split("-"))
                pages.extend(range(start - 1, end))
            else:
                pages.append(int(spec) - 1)

        # make a set of invalid page numbers
        wrong_pages = set([n + 1 for n in pages if n >= doc.page_count][:4])
        if wrong_pages != set():  # if any invalid numbers given, exit.
            sys.exit(f"Page number(s) {wrong_pages} not in '{doc}'.")

    # get the markdown string
    md_string = to_markdown(
        doc,
        pages=pages,
        write_images=True,
        force_text=False,
        image_path=r"C:\Users\haral\OneDrive\Desktop\pymupdf4llm\rag\pymupdf4llm\pymupdf4llm\helpers",
    )
    FILENAME = doc.name
    # output to a text file with extension ".md"
    outname = FILENAME.replace(".pdf", ".md")
    pathlib.Path(outname).write_bytes(md_string.encode())
    t1 = time.perf_counter()  # stop timer
    print(f"Markdown creation time for {FILENAME=} {round(t1-t0,2)} sec.")
