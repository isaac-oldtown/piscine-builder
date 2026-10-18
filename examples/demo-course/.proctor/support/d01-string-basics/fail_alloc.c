/* Allocation-failure shim. Student files are compiled with
 * -Dmalloc=sc_malloc, so every malloc they make goes through here.
 * fail_alloc_after(n): the n-th next allocation (0 = the next one) fails once. */
#include <stdlib.h>

static int countdown = -1;

void fail_alloc_after(int n)
{
    countdown = n;
}

void *sc_malloc(size_t size)
{
    if (countdown == 0) {
        countdown = -1;
        return NULL;
    }
    if (countdown > 0)
        countdown--;
    return malloc(size);
}
