package perso.finances

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import perso.finances.format.day
import perso.finances.format.euros
import perso.finances.format.instant
import perso.finances.format.monthLabel
import perso.finances.format.savingsRate
import perso.finances.format.shiftMonth
import perso.finances.format.stateLabel
import perso.finances.ui.normalizeServer

class FormatsTest {
    /** Les JDK récents séparent les milliers par une espace fine insécable : on compare sans. */
    private fun plain(text: String) = text.replace(' ', ' ').replace(' ', ' ')

    @Test
    fun `euros from integer cents, french style`() {
        assertEquals("1 234,56 €", plain(euros(123456)))
        assertEquals("-0,05 €", plain(euros(-5)))
        assertEquals("0,00 €", plain(euros(0)))
        assertEquals("92 233 720 368 547 758,07 €", plain(euros(Long.MAX_VALUE))) // aucun flottant
    }

    @Test
    fun `dates and instants in Paris time`() {
        assertEquals("26/09/2026", day("2026-09-26"))
        assertEquals("—", day(null))
        assertEquals("28/09/2026 à 21:25", instant("2026-09-28T19:25:00Z")) // UTC+2 l'été
        assertEquals("01/12/2026 à 09:00", instant("2026-12-01T08:00:00")) // UTC+1 l'hiver
        assertEquals("jamais", instant(null))
    }

    @Test
    fun `months and states`() {
        assertEquals("septembre 2026", monthLabel("2026-09"))
        assertEquals("2026-01", shiftMonth("2025-12", 1))
        assertEquals("2025-12", shiftMonth("2026-01", -1))
        assertEquals("94,5 %", savingsRate(945))
        assertEquals("-3,2 %", savingsRate(-32))
        assertEquals("—", savingsRate(null))
        assertEquals("impayé", stateLabel("impaye"))
    }

    @Test
    fun `server address must be https`() {
        assertEquals("https://pi.exemple.ts.net", normalizeServer(" pi.exemple.ts.net/ "))
        assertEquals("https://pi.exemple.ts.net", normalizeServer("https://pi.exemple.ts.net"))
        assertNull(normalizeServer("http://pi.exemple.ts.net"))
        assertNull(normalizeServer("https://"))
        assertNull(normalizeServer("pi exemple"))
        assertEquals("https://100.98.0.1:8443", normalizeServer("100.98.0.1:8443"))
    }
}
