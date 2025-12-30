from sys import argv
from sklearn.metrics import classification_report
from sklearn.metrics import f1_score
from sklearn.metrics import precision_score
from sklearn.metrics import recall_score
from sklearn.metrics import accuracy_score
from argparse import ArgumentParser

def readLinesFromFile(filePath):
    """Read lines from a file."""
    with open(filePath, 'r', encoding='utf-8') as fileRead:
        return [line.strip() for line in fileRead.readlines() if line.strip()]


def findPrecisionRecallF1score(goldLabels, predictedLabels, trueLabels=None):
    """Find Precision, Recall and F1 scores."""
    return classification_report(goldLabels,
                                 predictedLabels, target_names=trueLabels)


def main():
    """Pass arguments and call functions here."""
    parser = ArgumentParser()
    parser.add_argument('--ref', dest='ref', help="Add the input path of reference file")
    parser.add_argument('--pred', dest='hyp', help="Add the file path of predicted file")

    args = parser.parse_args()
    
    goldPath = args.ref
    predPath = args.hyp
    gold = readLinesFromFile(goldPath)
    predicted = readLinesFromFile(predPath)
    allLabels = set(predicted).union(set(gold))
    dictLabelToIndices = {label: index for index,
                          label in enumerate(allLabels)}
    predictedIntoIndexes = [dictLabelToIndices[item] for item in predicted]
    goldIntoIndexes = [dictLabelToIndices[item] for item in gold]
    classReport = ''
    classReport += findPrecisionRecallF1score(gold, predicted)
    if len(set(predictedIntoIndexes)) == 2:
        print('Micro Precision =', precision_score(goldIntoIndexes, predictedIntoIndexes, average='binary'))
        print('Micro Recall =', recall_score(goldIntoIndexes, predictedIntoIndexes, average='binary'))
        print('Micro F1 =', f1_score(goldIntoIndexes, predictedIntoIndexes, average='binary'))
        print('Micro Accuracy =', accuracy_score(goldIntoIndexes, predictedIntoIndexes))
    else:
        classReport += '\n'
        classReport += 'Micro_Precision = ' + str(precision_score(goldIntoIndexes, predictedIntoIndexes, average='micro')) + '\n'
        print('Micro Precision =', precision_score(goldIntoIndexes, predictedIntoIndexes, average='micro'))
        classReport += 'Micro_Recall = ' + str(recall_score(goldIntoIndexes, predictedIntoIndexes, average='micro')) + '\n'
        print('Micro Recall =', recall_score(goldIntoIndexes, predictedIntoIndexes, average='micro'))
        classReport += 'Micro_F1 = ' + str(f1_score(goldIntoIndexes, predictedIntoIndexes, average='micro')) + '\n'
        print('Micro F1 =', f1_score(goldIntoIndexes, predictedIntoIndexes, average='micro'))
        classReport += 'Micro_Accuracy = ' + str(accuracy_score(goldIntoIndexes, predictedIntoIndexes)) + '\n'
        print('Micro Accuracy =', accuracy_score(goldIntoIndexes, predictedIntoIndexes))


if __name__ == '__main__':
    main()
