package perso.finances

import org.junit.Assert.assertEquals
import org.junit.Test
import perso.finances.ui.Markdown
import perso.finances.ui.MdBlock

class MarkdownTest {
    @Test
    fun `blocks of a typical answer`() {
        val blocks = Markdown.parse(
            """
            ## Bilan de septembre

            Vous avez dépensé **153,69 €**,
            dont _18,50 €_ au restaurant.

            - Courses : 120 €
            - Restaurants : 18,50 €
              (deux repas)
            1. Premier
            2. Second

            | Catégorie | Montant |
            |---|---:|
            | Courses | 120 € |
            | Restaurants |

            > Données jusqu'au 5 octobre

            ```
            code
            ```
            ---
            """.trimIndent(),
        )
        assertEquals(
            listOf(
                MdBlock.Heading(2, "Bilan de septembre"),
                MdBlock.Paragraph("Vous avez dépensé **153,69 €**, dont _18,50 €_ au restaurant."),
                MdBlock.ListBlock(false, listOf("Courses : 120 €", "Restaurants : 18,50 € (deux repas)")),
                MdBlock.ListBlock(true, listOf("Premier", "Second")),
                MdBlock.Table(listOf("Catégorie", "Montant"), listOf(listOf("Courses", "120 €"), listOf("Restaurants", ""))),
                MdBlock.Quote("Données jusqu'au 5 octobre"),
                MdBlock.Code("code"),
                MdBlock.Rule,
            ),
            blocks,
        )
    }

    @Test
    fun `inline styles become plain text with spans, html stays text`() {
        val text = Markdown.inline("**Gras** et *italique*, `code`, ~~barré~~, [lien](https://x) <b>non</b> 2 * 3")
        assertEquals("Gras et italique, code, barré, lien <b>non</b> 2 * 3", text.text)
        assertEquals(4, text.spanStyles.size)
    }
}
