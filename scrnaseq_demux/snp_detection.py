from . import cellranger_specific
from .demux import ProbabilisticGenotypes, BarcodeHandler, Demultiplexer
from .snp_counter import count_call_variants_for_chromosome, count_snps, CompressedSNPCalls
import pysam
from joblib import Parallel, delayed
import numpy as np
import pandas as pd
from collections import defaultdict, Counter

from .utils import decompress_base


def detect_snps_for_chromosome(
        bamfile_path,
        chromosome,
        sorted_donors,
        barcode2donor: dict,
        discard_read,
        barcode_handler: BarcodeHandler,
        regularization: float,
        minimum_coverage: int,
        minimum_alternative_fraction: float = 0.01,
        minimum_alternative_coverage: int = 100,
        max_snp_candidates: int = 10000,
):
    # stage1. straightforward counting, to detect possible candidates for snp
    with pysam.AlignmentFile(bamfile_path) as bamfile:
        # size = 4 x positions (first axis enumerates (ACTG))
        coverage = np.asarray(
            bamfile.count_coverage(chromosome, read_callback=lambda read: not discard_read(read)),
            dtype="int32"
        )
        total = coverage.sum(axis=0)
        *_, alt, ref = np.sort(coverage, axis=0)
        is_candidate = (ref + alt) > minimum_coverage
        # prefer snps with only two alternatives
        is_candidate &= (ref + alt) > 0.98 * total
        is_candidate &= alt > minimum_alternative_coverage
        is_candidate &= alt > ref * minimum_alternative_fraction

        candidate_positions = np.where(is_candidate)[0]

        if len(candidate_positions) > max_snp_candidates:
            # TODO warn here?
            # if too many candidates (improbable), take ones with highest alternative count
            candidate_positions = np.argsort(alt * is_candidate)[-max_snp_candidates:]
            candidate_positions = np.sort(candidate_positions)

    # stage2. collect detailed counts about snp candidates
    # TODO optimization - minimize amount of barcodes passed here to those have donor associated?
    compressed_snp_calls = count_call_variants_for_chromosome(
        bamfile_path,
        chromosome=chromosome,
        chromosome_snps_zero_based=candidate_positions,
        cellbarcode_compressor=lambda cb: barcode_handler.barcode2index.get(cb, None),
        compute_p_read_misaligned=lambda read: 1e-4,
        discard_read=discard_read,
    )
    donor2dindex = {donor: dindex for dindex, donor in enumerate(sorted_donors)}

    position2donor2base2count = _count_snp_stats_for_donors(
        compressed_snp_calls, barcode_handler, barcode2donor, donor2dindex)

    # which positions are best?
    def importance_and_base_counts(counts):
        # counts : n_donors x 4
        # leaving two most important bases
        left_bases = alt, ref = np.argsort(counts.sum(axis=0))[-2:]
        base_counts = {
            'ACGT'[alt]: counts[:, alt].sum(),
            'ACGT'[ref]: counts[:, ref].sum(),
        }

        counts = counts[:, left_bases] + 1e-4
        # counts : n_donors x 2
        # how far each donor from average distribution and how confident we are about it?
        # 1 point = we are completely confident about one, and it's completely different from average
        # unreachable in practice, 0.4 is already very good
        count_0, count_1 = counts.sum(axis=0)
        p_1_avg = count_1 / (count_1 + count_0)
        p_1 = (counts[:, 1] + p_1_avg * regularization) / (counts.sum(axis=1) + regularization)
        # mse is importance of this position for each donor
        mse_for_each_donor = np.square(p_1_avg - p_1)
        return mse_for_each_donor, base_counts

    return [
        (chromosome, position) + importance_and_base_counts(counts)
        for position, counts in position2donor2base2count.items()
    ]


def _count_snp_stats_for_donors(compressed_snp_calls: CompressedSNPCalls, barcode_handler,
                                barcode2donor, donor2dindex,
                                max_contribution_to_base_count_from_barcode=3.):
    # computes bases at position for each donor given guesses for different barcodes
    # limits contribution
    calls = compressed_snp_calls.sindex2snp_call
    barcode_snp2counts = Counter()
    for mindex, reference_position, base_index, base_qual in calls[calls['p_base_wrong'] < 0.01]:
        cb_compressed, _ub, _p_group_misaligned = compressed_snp_calls.mindex2cb_ub_p_group_misaligned[mindex]
        barcode = barcode_handler.ordered_barcodes[cb_compressed]
        barcode_snp2counts[barcode, reference_position, decompress_base(base_index)] += 1

    position2donor2base2count = defaultdict(lambda: np.zeros([len(donor2dindex), 4], dtype='int32'))

    for (barcode, reference_position, base), count in barcode_snp2counts.items():
        donor = barcode2donor.get(barcode, None)
        if donor is None:
            continue
        contribution = min(max_contribution_to_base_count_from_barcode, count)
        position2donor2base2count[reference_position][donor2dindex[donor], 'ACGT'.index(base)] += contribution
    return position2donor2base2count


def detect_snps_positions(
        bamfile_location,
        genotypes: ProbabilisticGenotypes,
        barcode_handler: BarcodeHandler,
        minimum_coverage: int,
        minimum_alternative_fraction: float = 0.01,
        minimum_alternative_coverage: int = 100,
        n_best_snps_per_donor: int = 100,
        n_additional_best_snps: int = 1000,
        regularization: float = 3.,
        discard_read=cellranger_specific.discard_read,
        compute_p_read_misaligned=cellranger_specific.compute_p_misaligned,
        joblib_n_jobs=-1,
        result_beta_prior_filename=None,
        ignore_known_snps=True,
):
    """
    Detects SNPs based on data
    """
    # step1. complete dirty demultiplexing using known genotype
    snps = count_snps(
        bamfile_location=bamfile_location,
        chromosome2positions=genotypes.get_chromosome2positions(),
        barcode_handler=barcode_handler,
        joblib_n_jobs=joblib_n_jobs,
        discard_read=discard_read,
        compute_p_misaligned=compute_p_read_misaligned,
    )

    # returns two dataframes with likelihoods and posterior probabilities
    likelihoods, posterior_probabilities = Demultiplexer.predict_posteriors(
        snps,
        genotypes=genotypes,
        barcode_handler=barcode_handler,
        only_singlets=True,
    )
    barcode2donor = posterior_probabilities[posterior_probabilities.max(axis=1).gt(0.8)].idxmax(axis=1).to_dict()
    donor_counts = Counter(barcode2donor.values())
    for donor in genotypes.genotype_names:
        print('During inferring SNPs for', donor, 'will use', donor_counts[donor], 'barcodes')

    # step2. detect SNPs on
    with pysam.AlignmentFile(bamfile_location) as f:
        chromosomes = [x.contig for x in f.get_index_statistics()]

    sorted_donors = np.unique([donor for donor in barcode2donor.values()])

    with Parallel(n_jobs=joblib_n_jobs) as parallel:
        chrom_pos_importances_collection = parallel(
            delayed(detect_snps_for_chromosome)(
                bamfile_location,
                chromosome=chromosome,
                barcode2donor=barcode2donor,
                discard_read=discard_read,
                sorted_donors=sorted_donors,
                minimum_coverage=minimum_coverage,
                minimum_alternative_coverage=minimum_alternative_coverage,
                minimum_alternative_fraction=minimum_alternative_fraction,
                barcode_handler=barcode_handler,
                regularization=regularization,
            )
            for chromosome in chromosomes
        )
    chrom_pos_importances = sum(chrom_pos_importances_collection, [])
    selected_snps = _select_top_snps(chrom_pos_importances, n_additional_best_snps, n_best_snps_per_donor)
    # looks like it should spit out vcf file or beta coefficients. Probably beta coefficients.
    if ignore_known_snps:
        selected_snps = [
            (chrom, pos, importance, base_count)
            for chrom, pos, importance, base_count in selected_snps
            if not genotypes.contains_snp(chrom, pos)
        ]

    if result_beta_prior_filename is not None:
        _export_snps_to_beta(selected_snps, result_beta_prior_filename)

    return selected_snps


def _select_top_snps(chrom_pos_importances, n_additional_best_snps, n_best_snps_per_donor):
    importances_all = np.stack([imp for chrom, pos, imp, base_counts in chrom_pos_importances], axis=0)
    # selecting best for donors and best overall, merging them
    best_snps_for_donors = np.argsort(-importances_all, axis=0)[:n_best_snps_per_donor]
    best_snps_overall = np.argsort(-importances_all.sum(axis=1))
    is_new_snps = ~ np.isin(best_snps_overall, best_snps_for_donors)
    total_new_nps = np.cumsum(is_new_snps, axis=0)
    best_snps_overall = best_snps_overall[:np.searchsorted(total_new_nps, n_additional_best_snps, side='right')]
    selected_snp_ids = np.union1d(best_snps_for_donors.flatten(), best_snps_overall)
    return [chrom_pos_importances[i] for i in selected_snp_ids]


def _export_snps_to_beta(selected_snps, prior_filename):
    df = defaultdict(list)
    for chromosome, position, _importances, bases_count in selected_snps:
        for base, base_count in bases_count.items():
            df['CHROM'].append(chromosome)
            df['POS'].append(position)
            df['BASE'].append(base)
            df['DEFAULT_PRIOR'].append(base_count)

    pd.DataFrame(df).to_csv(prior_filename, sep='\t', index=False)