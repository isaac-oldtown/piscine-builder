use strict;
use warnings;

# use List::Util;  (a commented-out import must not count)
while (my $line = <STDIN>) {
    chomp $line;
    print scalar reverse($line), "\n";
}
